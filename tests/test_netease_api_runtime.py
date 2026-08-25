import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


class NeteaseApiRuntimeTest(unittest.IsolatedAsyncioTestCase):
    def test_resolve_api_dir_uses_repo_root_instead_of_src(self) -> None:
        from app.lifecycle.netease_api_runtime import NeteaseApiRuntime

        api_dir = NeteaseApiRuntime._resolve_api_dir("NeteaseAPI_tmp")

        self.assertEqual(api_dir, REPO_ROOT / "NeteaseAPI_tmp")

    async def test_start_skips_local_process_when_api_is_already_running(self) -> None:
        from app.lifecycle import netease_api_runtime as runtime_module

        runtime = runtime_module.NeteaseApiRuntime()
        with (
            patch.dict(
                runtime_module.NETEASE_CLOUD,
                {"auto_start_path": "NeteaseAPI_tmp"},
                clear=False,
            ),
            patch.object(runtime, "_api_is_ready", AsyncMock(return_value=True)),
            patch.object(
                runtime_module.asyncio,
                "create_subprocess_exec",
                new_callable=AsyncMock,
            ) as create_subprocess,
        ):
            await runtime.start()

        create_subprocess.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
