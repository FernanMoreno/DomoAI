import asyncio
import json
import tempfile
from pathlib import Path

from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings
from domoai.mcp.health import readyz


async def main():
    with tempfile.TemporaryDirectory() as d:
        r = await build_runtime(
            Settings(database_path=Path(d) / "state.db", audit_database_path=Path(d) / "audit.db")
        )
        try:
            await r.start()
            before = await readyz(r, None)
            t = r.lifecycle.task("domoai-scheduler")
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)
            after = await readyz(r, None)
            print(
                json.dumps(
                    {
                        "before": before.status_code,
                        "scheduler_done": t.done(),
                        "running_tasks": r.lifecycle.running_task_count,
                        "after": after.status_code,
                        "body": json.loads(after.body),
                    }
                )
            )
        finally:
            await r.close()


asyncio.run(main())
