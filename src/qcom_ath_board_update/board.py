from __future__ import annotations

import hashlib
import json
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .system import UpdateError


@dataclass
class BoardUpdate:
    action: str
    board_name: str
    bdf_md5: str
    bdf_sha256: str
    matching_names: list[str]


def file_hash(path: Path, algorithm: str = "sha256") -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def encoder_path(driver: str, override: Path | None = None) -> Path:
    if override:
        return override
    family = driver.removesuffix("_pci")
    installed = Path("/usr/libexec/qcom-ath-board-update") / f"{family}-bdencoder"
    if installed.is_file():
        return installed
    source = Path(__file__).resolve().parents[2] / "vendor" / f"{family}-bdencoder"
    if source.is_file():
        return source
    raise UpdateError(f"the bundled {family}-bdencoder could not be found")


def unpack_container(source: Path, destination: Path) -> Path:
    output = destination / "board-2.bin"
    if source.suffix == ".zst":
        with output.open("wb") as stream:
            result = subprocess.run(
                ["zstd", "-q", "-d", "-c", str(source)], stdout=stream, check=False
            )
        if result.returncode != 0:
            raise UpdateError(f"could not decompress {source}")
    else:
        shutil.copyfile(source, output)
    return output


def _run_encoder(encoder: Path, arguments: list[str], cwd: Path) -> None:
    result = subprocess.run(
        ["python3", str(encoder), *arguments], cwd=cwd, text=True, capture_output=True, check=False
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise UpdateError(f"{encoder.name} failed: {detail}")


def _preflight_container(path: Path, driver: str) -> None:
    data = path.read_bytes()
    family = driver.removesuffix("_pci")
    expected = f"QCA-{family.upper()}-BOARD".encode() + b"\0"
    if len(data) < 20 or data[: len(expected)] != expected:
        raise UpdateError(f"invalid {family} board-container signature")
    offset = 20
    output_names: set[tuple[int, str]] = set()
    while offset < len(data):
        if len(data) - offset < 8:
            raise UpdateError("truncated top-level board-container header")
        ie_id, ie_len = struct.unpack_from("<II", data, offset)
        if ie_id not in {0, 1}:
            raise UpdateError(f"unsupported top-level board-container element {ie_id}")
        offset += 8
        end = offset + ie_len
        if end > len(data):
            raise UpdateError("top-level board-container element exceeds file size")
        nested = offset
        first_name: str | None = None
        data_count = 0
        while nested < end:
            if end - nested < 8:
                raise UpdateError("truncated nested board-container header")
            nested_id, nested_len = struct.unpack_from("<II", data, nested)
            if nested_id not in {0, 1}:
                raise UpdateError(f"unsupported nested board-container element {nested_id}")
            nested += 8
            nested_end = nested + nested_len
            if nested_end > end:
                raise UpdateError("nested board-container element exceeds its parent")
            if nested_id == 0:
                try:
                    name = data[nested:nested_end].decode("utf-8")
                except UnicodeDecodeError as error:
                    raise UpdateError("board container contains a non-UTF-8 name") from error
                _validate_extraction_name(name)
                first_name = first_name or name
            else:
                data_count += 1
            nested = nested_end + (-nested_len % 4)
        if nested != end or first_name is None or data_count != 1:
            raise UpdateError("invalid board-container entry structure")
        output_key = (ie_id, first_name)
        if output_key in output_names:
            raise UpdateError(f"board container would extract duplicate filename for {first_name}")
        output_names.add(output_key)
        offset = end + (-ie_len % 4)
    if offset != len(data):
        raise UpdateError("board-container padding exceeds file size")


def _validate_extraction_name(name: str) -> None:
    if (
        not name
        or len(name.encode("utf-8")) > 240
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or any(ord(character) < 32 for character in name)
    ):
        raise UpdateError(f"unsafe board-container name: {name!r}")


def _load_mapping(directory: Path) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    try:
        mapping = json.loads((directory / "board-2.json").read_text(encoding="utf-8"))
        group = mapping[0]
        boards = group["board"]
    except (OSError, ValueError, IndexError, KeyError, TypeError) as error:
        raise UpdateError(f"invalid board-2.json produced by encoder: {error}") from error
    if not isinstance(mapping, list) or not isinstance(group, dict) or not isinstance(boards, list):
        raise UpdateError("invalid board-2.json structure produced by encoder")
    for entry in boards:
        if not isinstance(entry, dict) or not isinstance(entry.get("names"), list):
            raise UpdateError("invalid board entry produced by encoder")
        data = entry.get("data")
        if not isinstance(data, str):
            raise UpdateError("board entry has no data filename")
        path = (directory / data).resolve()
        if directory.resolve() not in path.parents or not path.is_file():
            raise UpdateError(f"unsafe or missing extracted board filename: {data}")
    return mapping, boards


def _inventory(directory: Path, mapping: list[dict[str, object]]) -> dict[str, dict[str, str]]:
    inventory: dict[str, dict[str, str]] = {"board": {}, "regdb": {}}
    group = mapping[0]
    for category in inventory:
        entries = group.get(category, [])
        if not isinstance(entries, list):
            raise UpdateError(f"invalid {category} mapping produced by encoder")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("names"), list):
                raise UpdateError(f"invalid {category} entry produced by encoder")
            payload = (directory / str(entry.get("data"))).read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            for name in entry["names"]:
                if not isinstance(name, str) or name in inventory[category]:
                    raise UpdateError(f"duplicate or invalid {category} name: {name!r}")
                inventory[category][name] = digest
    return inventory


def _analyze(directory: Path, boards: list[dict[str, object]], board_name: str, bdf: bytes) -> BoardUpdate:
    identical: dict[str, object] | None = None
    matching_names: list[str] = []
    target_matches: list[tuple[list[object], bytes]] = []
    for entry in boards:
        names = entry["names"]
        assert isinstance(names, list)
        data = (directory / str(entry["data"])).read_bytes()
        if board_name in names:
            target_matches.append((names, data))
        if data == bdf and identical is None:
            identical = entry
            matching_names = [str(name) for name in names]
    if target_matches:
        if len(target_matches) != 1:
            raise UpdateError("the target board name occurs more than once in the container")
        names, data = target_matches[0]
        if data != bdf:
            raise UpdateError(
                "the target board name already exists with different calibration data; refusing to replace it"
            )
        return BoardUpdate("none", board_name, _md5(bdf), _sha256(bdf), [str(name) for name in names])
    action = "alias" if identical else "add"
    return BoardUpdate(action, board_name, _md5(bdf), _sha256(bdf), matching_names)


def inspect_update(container: Path, driver: str, board_name: str, bdf_path: Path, encoder: Path | None = None) -> BoardUpdate:
    selected_encoder = encoder_path(driver, encoder)
    bdf = bdf_path.read_bytes()
    with tempfile.TemporaryDirectory(prefix="qcom-ath-board-update-") as temporary:
        directory = Path(temporary)
        unpacked = unpack_container(container, directory)
        _preflight_container(unpacked, driver)
        _run_encoder(selected_encoder, ["--extract", str(unpacked)], directory)
        _, boards = _load_mapping(directory)
        return _analyze(directory, boards, board_name, bdf)


def build_update(
    container: Path,
    driver: str,
    board_name: str,
    bdf_path: Path,
    output: Path,
    encoder: Path | None = None,
) -> BoardUpdate:
    selected_encoder = encoder_path(driver, encoder)
    bdf = bdf_path.read_bytes()
    with tempfile.TemporaryDirectory(prefix="qcom-ath-board-update-") as temporary:
        directory = Path(temporary)
        unpacked = unpack_container(container, directory)
        _preflight_container(unpacked, driver)
        _run_encoder(selected_encoder, ["--extract", str(unpacked)], directory)
        mapping, boards = _load_mapping(directory)
        original_inventory = _inventory(directory, mapping)
        update = _analyze(directory, boards, board_name, bdf)
        if update.action == "none":
            return update
        if update.action == "alias":
            for entry in boards:
                data = (directory / str(entry["data"])).read_bytes()
                if data == bdf:
                    names = entry["names"]
                    assert isinstance(names, list)
                    names.append(board_name)
                    break
        else:
            filename = "qcom-ath-board-update-new.bin"
            (directory / filename).write_bytes(bdf)
            boards.append({"names": [board_name], "data": filename})
        mapping_path = directory / "board-2.json"
        mapping_path.write_text(json.dumps(mapping, indent=4) + "\n", encoding="utf-8")
        rebuilt = directory / "rebuilt-board-2.bin"
        _run_encoder(selected_encoder, ["--create", str(mapping_path), "--output", str(rebuilt)], directory)
        _verify(rebuilt, selected_encoder, driver, board_name, bdf, original_inventory)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(rebuilt, output)
        return update


def _verify(
    container: Path,
    encoder: Path,
    driver: str,
    board_name: str,
    bdf: bytes,
    original_inventory: dict[str, dict[str, str]],
) -> None:
    with tempfile.TemporaryDirectory(prefix="qcom-ath-board-verify-") as temporary:
        directory = Path(temporary)
        _preflight_container(container, driver)
        _run_encoder(encoder, ["--extract", str(container)], directory)
        mapping, boards = _load_mapping(directory)
        rebuilt_inventory = _inventory(directory, mapping)
        for category, entries in original_inventory.items():
            for name, digest in entries.items():
                if rebuilt_inventory[category].get(name) != digest:
                    raise UpdateError(f"rebuilt container changed or removed {category} entry {name}")
        matches = []
        for entry in boards:
            names = entry["names"]
            assert isinstance(names, list)
            if board_name in names:
                matches.append((directory / str(entry["data"])).read_bytes())
        if len(matches) != 1 or matches[0] != bdf:
            raise UpdateError("rebuilt board container failed round-trip verification")


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()  # noqa: S324 - compatibility identifier only


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
