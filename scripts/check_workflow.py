"""检查 workflow 里所有 bash 片段能否通过语法解析。"""

from pathlib import Path
import os
import subprocess
import tempfile

import yaml

WORKFLOW = Path(".github/workflows/sync-wiki.yml")
BASH = r"D:\Git\Git\bin\bash.exe"


def main() -> int:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = data["jobs"]["sync"]["steps"]

    print(f"workflow: {data.get('name')}")
    print(f"steps   : {len(steps)}")
    print()

    failures = 0
    for step in steps:
        script = step.get("run")
        if not script:
            continue

        name = step.get("name")
        with tempfile.NamedTemporaryFile(
            "w", suffix=".sh", delete=False, encoding="utf-8", newline="\n"
        ) as handle:
            handle.write(script)
            path = handle.name

        try:
            result = subprocess.run(
                [BASH, "-n", path], capture_output=True, text=True
            )
        finally:
            os.unlink(path)

        if result.returncode == 0:
            print(f"  [OK]   {name}")
        else:
            failures += 1
            print(f"  [FAIL] {name}")
            print(f"         {result.stderr.strip()[:200]}")

    print()
    if failures:
        print(f"结果: {failures} 个片段语法失败")
        return 1
    print("结果: 全部 bash 片段语法正确")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
