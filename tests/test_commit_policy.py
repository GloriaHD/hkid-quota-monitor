"""提交节流策略：1 分钟 cron 后不能每轮都提交（Pages 构建软限 +
jsDelivr purge 配额都会被打满——后者实测把镜像冻了 6 小时）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quota_monitor.run import COMMIT_SPACING_MIN, should_commit


def test_plain_change_waits_for_spacing():
    assert not should_commit(True, 0, since_commit_min=1.0,
                             heartbeat_due=False, first_run=False)
    assert should_commit(True, 0, since_commit_min=COMMIT_SPACING_MIN,
                         heartbeat_due=False, first_run=False)


def test_release_event_commits_immediately():
    """有放号事件必须立即提交：通知已发出，冷却状态 notify_state 不落库
    的话，下一轮重新 diff 出同一批事件会对订阅者重复轰炸。"""
    assert should_commit(True, 1, since_commit_min=0.5,
                         heartbeat_due=False, first_run=False)


def test_no_change_no_commit_unless_heartbeat():
    assert not should_commit(False, 0, since_commit_min=99.0,
                             heartbeat_due=False, first_run=False)
    assert should_commit(False, 0, since_commit_min=25.0,
                         heartbeat_due=True, first_run=False)


def test_first_run_always_commits():
    assert should_commit(False, 0, since_commit_min=0.0,
                         heartbeat_due=False, first_run=True)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
