#!/usr/bin/env python3
"""Interactive build and flash helper for STM32CubeMX CMake projects."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ELECTRICAL = ROOT / "Electrical"
CONFIG_ROOT = ELECTRICAL / "config"


@dataclass(frozen=True)
class BuildTarget:
    category: str
    board_project: str
    project_name: str
    project_dir: Path
    ioc_file: Path
    board: str
    mcu: str
    firmware_package: str


def choose(prompt: str, options: list[tuple[str, object]]) -> object:
    """Show numbered options and return the selected value; Enter asks again."""
    if not options:
        raise RuntimeError(f"No options found for {prompt.lower()}.")

    print(f"\n{prompt}")
    for index, (label, _value) in enumerate(options, start=1):
        print(f"  {index}) {label}")

    while True:
        answer = input(f"Choice [1-{len(options)}]: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1][1]
        print(f"Enter a number from 1 to {len(options)}.")


def read_ioc_metadata(ioc_file: Path) -> dict[str, str]:
    """Read key/value metadata from a CubeMX .ioc file."""
    metadata = {}
    for line in ioc_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            metadata[key] = value
    return metadata


def is_nucleo_target(project_dir: Path, metadata: dict[str, str]) -> bool:
    """Classify a target as Nucleo from its folder or CubeMX board metadata."""
    path_names = (part.casefold() for part in project_dir.relative_to(CONFIG_ROOT).parts)
    board_name = metadata.get("BSP_IP_NAME", "").casefold()
    return "nucleo" in board_name or any("nucleo" in part for part in path_names)


def custom_board_name(project_dir: Path, mcu: str) -> str:
    """Get the board folder name from config/<mcu>/<board>/<config> layouts."""
    parts = list(project_dir.relative_to(CONFIG_ROOT).parts)
    if parts and parts[0].casefold() in {"mcu", "mcus"}:
        parts.pop(0)

    # Some layouts put the MCU part number before the board folder.
    while parts and parts[0].upper().startswith("STM32") and mcu.upper().startswith(parts[0].upper()):
        parts.pop(0)

    if not parts:
        return project_dir.name
    return parts[0] if len(parts) > 1 else parts[0]


def discover_targets() -> list[BuildTarget]:
    """Find CubeMX targets beneath Electrical/config and classify them."""
    if not CONFIG_ROOT.is_dir():
        raise RuntimeError(f"Configuration folder not found: {CONFIG_ROOT}")

    targets = []
    for preset_file in sorted(CONFIG_ROOT.rglob("CMakePresets.json")):
        project_dir = preset_file.parent
        if not (project_dir / "CMakeLists.txt").is_file():
            continue

        for ioc_file in sorted(project_dir.glob("*.ioc")):
            try:
                metadata = read_ioc_metadata(ioc_file)
            except OSError as error:
                raise RuntimeError(f"Could not read {ioc_file}: {error}") from error

            mcu = metadata.get("Mcu.CPN") or metadata.get("Mcu.Name", "MCU not specified")
            nucleo = is_nucleo_target(project_dir, metadata)
            board = metadata.get("BSP_IP_NAME", "").strip()
            if not board:
                board = next(
                    (part for part in project_dir.relative_to(CONFIG_ROOT).parts if "nucleo" in part.casefold()),
                    project_dir.name,
                )
            board_project = "Nucleo configurations" if nucleo else custom_board_name(project_dir, mcu)
            firmware_package = metadata.get("ProjectManager.FirmwarePackage", "").strip()

            targets.append(
                BuildTarget(
                    category="nucleo" if nucleo else "mcu",
                    board_project=board_project,
                    project_name=project_dir.name,
                    project_dir=project_dir,
                    ioc_file=ioc_file,
                    board=board,
                    mcu=mcu,
                    firmware_package=firmware_package,
                )
            )

    return targets


def find_build_presets(project_dir: Path) -> list[tuple[str, tuple[str, str]]]:
    """Return (display name, configure preset and build preset) choices."""
    preset_path = project_dir / "CMakePresets.json"
    try:
        data = json.loads(preset_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read {preset_path}: {error}") from error

    configure_names = {
        preset["name"]
        for preset in data.get("configurePresets", [])
        if "name" in preset and not preset.get("hidden", False)
    }
    choices = []
    for preset in data.get("buildPresets", []):
        name = preset.get("name")
        configure_name = preset.get("configurePreset")
        if name and configure_name in configure_names:
            choices.append((name, (configure_name, name)))

    if not choices:
        raise RuntimeError(f"No usable configure/build presets found in {preset_path}.")
    return choices


def run_cube(project_dir: Path, *arguments: str) -> None:
    """Run one cube command from the selected generated project directory."""
    print(f"\n$ cube {' '.join(arguments)}")
    subprocess.run(["cube", *arguments], cwd=project_dir, check=True)


def find_elf(project_dir: Path, build_preset: str) -> Path:
    """Find the firmware ELF emitted for the selected CMake build preset."""
    build_dir = project_dir / "build" / build_preset
    elf_files = sorted(build_dir.rglob("*.elf")) if build_dir.is_dir() else []
    if not elf_files:
        raise RuntimeError(f"No .elf file found under {build_dir}; the build may not have produced firmware.")
    if len(elf_files) > 1:
        names = ", ".join(str(path.relative_to(project_dir)) for path in elf_files)
        raise RuntimeError(f"More than one .elf file found under {build_dir}: {names}")
    return elf_files[0]


def target_label(target: BuildTarget) -> str:
    return f"{target.project_name} — {target.board} / {target.mcu} ({target.ioc_file.name})"


def select_target(targets: list[BuildTarget], category: str) -> BuildTarget:
    """Show all Nucleo configs, or select a custom MCU board and its config."""
    category_targets = [target for target in targets if target.category == category]
    if not category_targets:
        if category == "nucleo":
            raise RuntimeError(
                f"No Nucleo CMake targets found under {CONFIG_ROOT}. Generate a CubeMX CMake project there first."
            )
        raise RuntimeError(
            f"No custom MCU board CMake targets found under {CONFIG_ROOT}. "
            "Add each board's CubeMX project under its MCU configuration folder first."
        )

    if category == "nucleo":
        return choose(
            "Which Nucleo configuration do you want to build?",
            [(target_label(target), target) for target in category_targets],
        )

    board_names = sorted({target.board_project for target in category_targets})
    selected_board = choose(
        "Which custom MCU board do you want to build?",
        [(name, name) for name in board_names],
    )
    board_targets = [target for target in category_targets if target.board_project == selected_board]
    if len(board_targets) == 1:
        return board_targets[0]

    return choose(
        f"Which configuration for {selected_board}?",
        [(target_label(target), target) for target in board_targets],
    )


def main() -> None:
    targets = discover_targets()
    nucleo_count = sum(target.category == "nucleo" for target in targets)
    mcu_boards = sorted({target.board_project for target in targets if target.category == "mcu"})

    category = choose(
        "What are you building for?",
        [
            (f"Nucleo ({nucleo_count} configurations)", "nucleo"),
            (f"Custom MCU board ({len(mcu_boards)} configured boards)", "mcu"),
        ],
    )
    target = select_target(targets, category)

    if category == "mcu":
        package = target.firmware_package or "No firmware package recorded in this .ioc"
        print(f"\nFirmware package recommendation for {target.mcu}: {package}")
        print(f"This recommendation comes from {target.ioc_file.name}; change the package in CubeMX and regenerate if needed.")

    presets = dict(
        (name, preset_pair)
        for name, preset_pair in find_build_presets(target.project_dir)
    )
    if "Debug" not in presets:
        raise RuntimeError(f"The selected target has no Debug build preset: {target.project_dir}")
    configure_preset, build_preset = presets["Debug"]
    print("\nBuild configuration: Debug")
    action = choose(
        "What do you want to do?",
        [
            ("Build only", "build"),
            ("Build and flash to the connected ST-LINK board (SWD)", "flash"),
        ],
    )

    print(f"\nBuilding {target.project_name} for {target.board} / {target.mcu} ({build_preset})")
    run_cube(target.project_dir, "cmake", "--preset", configure_preset)
    run_cube(target.project_dir, "cmake", "--build", "--preset", build_preset)

    if action == "flash":
        elf_file = find_elf(target.project_dir, build_preset)
        print(f"\nFlashing {elf_file.relative_to(target.project_dir)} over SWD")
        run_cube(
            target.project_dir,
            "programmer",
            "-c",
            "port=SWD",
            "-w",
            str(elf_file),
            "-v",
            "-rst",
        )

    print("\nDone.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Error: {error}") from error
    except KeyboardInterrupt:
        raise SystemExit("\nCancelled.")
