# RealStop 0.2.3

This release combines the reviewed parallel-tool ancestry correction with a packaged experimental Supervisor library. Existing rules adapters retain their behavior. Confirmed hooks and Supervisor remain opt-in; no model weights are changed or bundled.

## Install

Install the wheel from the [GitHub release](https://github.com/decision-tune/realstop/releases/tag/v0.2.3) with `pip install /path/to/realstop-0.2.3-py3-none-any.whl`, or use `uv tool install /path/to/realstop-0.2.3-py3-none-any.whl` for the CLI. Verify with `realstop --version`. A GitHub asset is not evidence of a PyPI upload: use `pip install realstop==0.2.3` only when that version is available on PyPI.

For library use, install into your Python environment and run `python -m realstop.supervisor --help`. See the [Supervisor guide](../subprojects/supervisor/README.md). Its public API is experimental and may change. Contract declarations do not prove complete authority; artifact observations are point-in-time evidence and require revalidation before any future action.

Confirmed hooks have a separate dry-run installer and rollback process; see [confirmed hooks](confirmed-release.md). Installing or upgrading the Python package alone does not replace an immutable confirmed-hook installation. Codex hook trust still requires the normal host review.

## Evidence and limits

- The parser correction accepts only correlated tool-result branches and rejects stale or ambiguous references. It does not make compacted, incomplete or oversized histories eligible.
- The earlier seven-hour observation recorded 36 ancestry abstentions and no native-verifier calls. It establishes a coverage failure in that session, not protection or accuracy.
- Public artifact fixtures calibrate file checks, not general reasoning accuracy. A controlled synthetic mechanism test exercised one native-confirmed advisory claim and two zero-call controls. No continuation action was executed.
- Supervisor is not wired into production Stop hooks. The release makes the components importable and reviewable; it does not establish a real-world performance improvement.

See [tool receipt ancestry](tool-receipt-branches.md), [monitoring](live-monitoring.md), [evaluation](../subprojects/supervisor/EVALUATION.md), and [license inventory](../subprojects/supervisor/REUSE.md).
