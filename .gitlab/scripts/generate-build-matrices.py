import itertools
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

EXTENSION_REGEX = re.compile(r"^src/(?P<lang>\w+)/(?P<extension>\w+)")
MULTISRC_LIB_REGEX = re.compile(r"^lib-multisrc/(?P<multisrc>\w+)")
LIB_REGEX = re.compile(r"^lib/(?P<lib>\w+)")
MODULE_REGEX = re.compile(r"^:src:(?P<lang>\w+):(?P<extension>\w+)$")
CORE_FILES_REGEX = re.compile(
    r"^(buildSrc/|core/|gradle/|build\.gradle\.kts|common\.gradle|gradle\.properties|settings\.gradle\.kts|\.gitlab/scripts|\.gitlab-ci\.yml)"
)


def run_command(command: str) -> str:
    result = subprocess.run(command, capture_output=True, text=True, shell=True)
    if result.returncode != 0:
        print(result.stderr.strip())
        sys.exit(result.returncode)
    return result.stdout.strip()


def resolve_dependent_libs(libs: set[str]) -> set[str]:
    """All libs that (transitively) depend on any of the passed libs."""
    if not libs:
        return set()

    all_dependent_libs: set[str] = set()
    to_process = set(libs)

    while to_process:
        current_libs = to_process
        to_process = set()

        lib_dependency = re.compile(
            rf"project\([\"']:(?:lib):({'|'.join(map(re.escape, current_libs))})[\"']\)"
        )

        for lib in Path("lib").iterdir():
            if lib.name in all_dependent_libs or lib.name in libs:
                continue

            build_file = lib / "build.gradle.kts"
            if not build_file.is_file():
                continue

            if lib_dependency.search(build_file.read_text("utf-8")):
                all_dependent_libs.add(lib.name)
                to_process.add(lib.name)

    return all_dependent_libs


def resolve_multisrc_lib(libs: set[str]) -> set[str]:
    """All multisrc modules that depend on any of the passed libs."""
    if not libs:
        return set()

    lib_dependency = re.compile(
        rf"project\([\"']:(?:lib):({'|'.join(map(re.escape, libs))})[\"']\)"
    )

    multisrcs: set[str] = set()
    for multisrc in Path("lib-multisrc").iterdir():
        build_file = multisrc / "build.gradle.kts"
        if not build_file.is_file():
            continue
        if lib_dependency.search(build_file.read_text("utf-8")):
            multisrcs.add(multisrc.name)

    return multisrcs


def resolve_ext(multisrcs: set[str], libs: set[str]) -> set[tuple[str, str]]:
    """All extensions that depend on any of the passed multisrcs or libs."""
    if not multisrcs and not libs:
        return set()

    patterns = []
    if multisrcs:
        patterns.append(rf"themePkg\s*=\s*['\"]({'|'.join(map(re.escape, multisrcs))})['\"]")
    if libs:
        patterns.append(rf"project\([\"']:(?:lib):({'|'.join(map(re.escape, libs))})[\"']\)")

    regex = re.compile('|'.join(patterns))

    extensions: set[tuple[str, str]] = set()
    for lang in Path("src").iterdir():
        for extension in lang.iterdir():
            build_file = extension / "build.gradle"
            if not build_file.is_file():
                continue
            if regex.search(build_file.read_text("utf-8")):
                extensions.add((lang.name, extension.name))

    return extensions


def get_module_list(ref: str) -> tuple[list[str], list[str]]:
    diff_output = run_command(f"git diff --name-status {ref}").splitlines()

    changed_files = [
        file
        for line in diff_output
        for file in line.split("\t", 2)[1:]
    ]

    modules: set[str] = set()
    multisrcs: set[str] = set()
    libs: set[str] = set()
    deleted: set[str] = set()
    core_files_changed = False

    for file in map(lambda x: Path(x).as_posix(), changed_files):
        if CORE_FILES_REGEX.search(file):
            core_files_changed = True

        elif match := EXTENSION_REGEX.search(file):
            lang = match.group("lang")
            extension = match.group("extension")
            if Path("src", lang, extension).is_dir():
                modules.add(f':src:{lang}:{extension}')
            deleted.add(f"{lang}.{extension}")

        elif match := MULTISRC_LIB_REGEX.search(file):
            multisrc = match.group("multisrc")
            if Path("lib-multisrc", multisrc).is_dir():
                multisrcs.add(multisrc)

        elif match := LIB_REGEX.search(file):
            lib = match.group("lib")
            if Path("lib", lib).is_dir():
                libs.add(lib)

    if core_files_changed:
        all_modules, all_deleted = get_all_modules()
        modules.update(all_modules)
        deleted.update(all_deleted)
        return list(modules), list(deleted)

    libs.update(resolve_dependent_libs(libs))
    multisrcs.update(resolve_multisrc_lib(libs))

    extensions = resolve_ext(multisrcs, libs)
    modules.update(f":src:{lang}:{extension}" for lang, extension in extensions)
    deleted.update(f"{lang}.{extension}" for lang, extension in extensions)

    if os.getenv("IS_PR_CHECK") != "true":
        always_build_file = Path(".gitlab/always_build.json")
        if always_build_file.is_file():
            for extension in json.loads(always_build_file.read_text("utf-8")):
                modules.add(":src:" + extension.replace(".", ":"))
                deleted.add(extension)

    return list(modules), list(deleted)


def get_all_modules() -> tuple[list[str], list[str]]:
    modules = []
    deleted = []
    for lang in Path("src").iterdir():
        for extension in lang.iterdir():
            modules.append(f":src:{lang.name}:{extension.name}")
            deleted.append(f"{lang.name}.{extension.name}")
    return modules, deleted


def write_child_pipeline(chunks: list[list[str]], out_path: str = "build-pipeline.yml") -> None:
    """Emit a GitLab child-pipeline YAML that runs each chunk as a parallel job."""
    # Optional runner tags, set via BUILD_RUNNER_TAGS env var ("mytag" or "tag-a,tag-b").
    tag_env = os.getenv("BUILD_RUNNER_TAGS", "").strip()
    runner_tags = [t.strip() for t in tag_env.split(",") if t.strip()]

    lines: list[str] = [
        "# Auto-generated by generate-build-matrices.py",
        "stages:",
        "  - build",
        "",
        ".build-template:",
        "  stage: build",
        "  image: eclipse-temurin:17-jdk",
        "  timeout: 30 minutes",
        "  interruptible: true",
    ]

    if runner_tags:
        lines.append("  tags:")
        lines.extend(f"    - {t}" for t in runner_tags)

    lines += [
        "  variables:",
        # Shallow clone — build jobs don't need history, and full-depth clones
        # from every chunk simultaneously tend to overload GitLab's git server.
        '    GIT_DEPTH: "1"',
        '    GRADLE_USER_HOME: "$CI_PROJECT_DIR/.gradle"',
        '    GRADLE_OPTS: "-Dorg.gradle.daemon=false"',
        "  retry:",
        "    max: 2",
        "    when:",
        "      - runner_system_failure",
        "      - stuck_or_timeout_failure",
        "      - scheduler_failure",
        "      - api_failure",
        "      - unknown_failure",
        "  cache:",
        "    key: gradle-$CI_COMMIT_REF_SLUG",
        "    paths:",
        "      - .gradle/caches",
        "      - .gradle/wrapper",
        "  before_script:",
        '    - echo "$SIGNING_KEY" | base64 -d > signingkey.jks',
        "  script:",
        "    - ./gradlew $MODULES",
        "  after_script:",
        "    - rm -f signingkey.jks",
        "  artifacts:",
        "    when: on_success",
        "    paths:",
        '      - "**/*.apk"',
        "    expire_in: 1 day",
        "",
    ]

    if not chunks:
        # Pipeline must be non-empty — emit a no-op so the parent trigger succeeds.
        lines += [
            "noop:",
            "  stage: build",
            "  image: busybox",
            "  script:",
            '    - echo "Nothing to build."',
            "",
        ]
    else:
        for i, chunk in enumerate(chunks, start=1):
            module_arg = " ".join(chunk)
            lines += [
                f"build-chunk-{i}:",
                "  extends: .build-template",
                "  variables:",
                # json.dumps gives us a safely-quoted YAML-compatible string.
                f"    MODULES: {json.dumps(module_arg)}",
                "",
            ]

    Path(out_path).write_text("\n".join(lines), encoding="utf-8")


def main() -> NoReturn:
    _, ref, build_type = sys.argv
    modules, deleted = get_module_list(ref)

    chunk_size = int(os.getenv("CI_CHUNK_SIZE", 65))
    gradle_tasks = [f"{m}:assemble{build_type}" for m in modules]
    chunks = [list(c) for c in itertools.batched(gradle_tasks, chunk_size)]

    print(f"Modules to build ({len(gradle_tasks)}) in {len(chunks)} chunk(s).")
    print(f"Modules to delete ({len(deleted)}): {json.dumps(deleted)}")

    if os.getenv("CI") == "true":
        write_child_pipeline(chunks)
        Path("modules-delete.json").write_text(json.dumps(deleted), encoding="utf-8")

    sys.exit(0)


if __name__ == '__main__':
    main()
