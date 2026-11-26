"""用 git 凭据管理器里的 PAT 创建正确的 WIKI_TOKEN secret。

背景：用户在 GitHub 网页手动创建 secret 时，名字误写成 `IKI_TOKEN`（少了开头的 W）。
工作流读的是 `WIKI_TOKEN`，所以始终判定为"缺少凭据"。

本脚本：
1. 从 git credential manager 取出已有 PAT（不打印）
2. 取仓库 public key
3. 用 libsodium sealed box 加密（GitHub 要求）
4. 创建名为 WIKI_TOKEN 的 secret

不做任何删除动作，只新增。
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from nacl import encoding, public

OWNER = "AJ-zj"
REPO = "power_copy"
SECRET_NAME = "WIKI_TOKEN"
API = "https://api.github.com"

# git 可能不在 PATH 里，按常见位置回退查找
GIT_CANDIDATES = [
    "git",
    r"D:\Git\Git\cmd\git.exe",
    r"C:\Program Files\Git\cmd\git.exe",
    r"C:\Program Files (x86)\Git\cmd\git.exe",
]


def find_git() -> str:
    import shutil

    for candidate in GIT_CANDIDATES:
        if candidate == "git":
            found = shutil.which("git")
            if found:
                return found
        elif Path(candidate).is_file():
            return candidate
    raise RuntimeError("未找到 git，请安装 Git 或确认安装路径")


def get_token_from_git() -> str:
    """从 git 凭据助手取 token。用 stdin 喂协议信息，避免交互。"""

    git = find_git()
    payload = "protocol=https\nhost=github.com\n\n"
    result = subprocess.run(
        [git, "credential", "fill"],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError("git credential fill 失败")

    for line in result.stdout.splitlines():
        if line.startswith("password="):
            return line[len("password=") :]
    raise RuntimeError("凭据里没有 password 字段")


def api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body else None
    request = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Content-Type": "application/json"} if data else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {error.code}: {detail}") from error


def encrypt(public_key_b64: str, secret_value: str) -> str:
    """用 libsodium sealed box 加密 secret。"""

    key = public.PublicKey(public_key_b64.encode(), encoding.Base64Encoder())
    sealed = public.SealedBox(key)
    encrypted = sealed.encrypt(secret_value.encode())
    return base64.b64encode(encrypted).decode()


def main() -> int:
    print("=== 读取 git 凭据 ===")
    token = get_token_from_git()
    print(f"  token 长度: {len(token)}（不打印内容）")

    print()
    print("=== 验证 token 权限 ===")
    try:
        user = api("GET", "/user", token)
        print(f"  身份: {user.get('login')}")
    except RuntimeError as error:
        print(f"  失败: {error}")
        return 1

    print()
    print("=== 当前仓库 secrets ===")
    existing = api("GET", f"/repos/{OWNER}/{REPO}/actions/secrets", token)
    names = [s["name"] for s in existing.get("secrets", [])]
    print(f"  总数: {existing.get('total_count', 0)}")
    for name in names:
        marker = "  ← 拼写错误，工作流需要的是 WIKI_TOKEN" if name == "IKI_TOKEN" else ""
        print(f"    - {name}{marker}")

    if SECRET_NAME in names:
        print()
        print(f"  {SECRET_NAME} 已存在，将更新其值")

    print()
    print("=== 取仓库 public key ===")
    key_info = api(
        "GET", f"/repos/{OWNER}/{REPO}/actions/secrets/public-key", token
    )
    key_id = key_info["key_id"]
    print(f"  key_id: {key_id}")

    print()
    print("=== 加密并创建 secret ===")
    encrypted = encrypt(key_info["key"], token)
    api(
        "PUT",
        f"/repos/{OWNER}/{REPO}/actions/secrets/{SECRET_NAME}",
        token,
        {"encrypted_value": encrypted, "key_id": key_id},
    )
    print(f"  已创建 {SECRET_NAME}")

    print()
    print("=== 最终确认 ===")
    final = api("GET", f"/repos/{OWNER}/{REPO}/actions/secrets", token)
    final_names = [s["name"] for s in final.get("secrets", [])]
    print(f"  secret 总数: {final.get('total_count', 0)}")
    for name in final_names:
        ok = " ✅" if name == SECRET_NAME else ""
        print(f"    - {name}{ok}")

    if SECRET_NAME in final_names:
        print()
        print(f"结果: {SECRET_NAME} 配置成功，工作流可以正常推送 Wiki")
        if "IKI_TOKEN" in final_names:
            print()
            print("提示: IKI_TOKEN 是拼写错误留下的无效 secret，")
            print("      可在 https://github.com/AJ-zj/power_copy/settings/secrets/actions 手动删除")
        return 0

    print()
    print("结果: 创建失败")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
