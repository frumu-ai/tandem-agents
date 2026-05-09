import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.aca.config.config import resolve_config
from src.aca.core.engine.tandem_client_sdk import (
    sdk_browser_close,
    sdk_browser_extract,
    sdk_browser_open,
    sdk_browser_screenshot,
    sdk_browser_snapshot,
)


def normalize_url(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme:
        return text
    return f"https://{text}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="test_browser")
    parser.add_argument("urls", nargs="*", help="URLs to test")
    parser.add_argument("--url", dest="urls_flag", action="append", default=[], help="Additional URL (repeatable)")
    parser.add_argument("--deep", action="store_true", help="Also run snapshot and extract checks (best-effort)")
    return parser.parse_args(argv)


def check_browser_status(cfg):
    try:
        from tandem_client import SyncTandemClient

        token = cfg.tandem_token()
        base = cfg.tandem.base_url if cfg.tandem else "http://tandem-engine:39733"
        c = SyncTandemClient(token=token, base_url=base)
        s = c.browser.status()
        print("Browser status:")
        print(f"  enabled : {s.enabled}")
        print(f"  runnable: {s.runnable}")
        print(f"  browser : found={s.browser.found} path={s.browser.path}")
        print(f"  sidecar : found={s.sidecar.found} path={getattr(s.sidecar, 'path', None)}")
        if s.blocking_issues:
            print("  issues  :")
            for issue in s.blocking_issues:
                print(f"    - {issue.code}: {issue.message}")
        return s
    except Exception as e:
        print(f"Status check failed: {e}")
        return None


def run_one_url(cfg, url: str, deep: bool = False) -> bool:
    print(f"\nOpening {url}...")
    session_id = None
    try:
        result = sdk_browser_open(cfg, url)
        if not isinstance(result, dict):
            print(f"  open returned non-dict: {result}")
            return False
        session_id = result.get("session_id")
        print(f"  session_id: {session_id}")
        print(f"  final_url: {result.get('final_url', 'n/a')}")
        print(f"  title: {result.get('title', 'n/a')}")

        print("\nTaking screenshot...")
        shot = sdk_browser_screenshot(cfg, session_id, full_page=True)
        print(f"  screenshot: {shot}")
        if isinstance(shot, dict):
            artifact = shot.get("artifact") or {}
            uri = artifact.get("uri")
            bytes_count = ((artifact.get("metadata") or {}).get("bytes") or 0)
            print(f"  screenshot bytes: {bytes_count}")
            if uri:
                print(f"  screenshot file in engine container: {uri}")
                if "/home/node/.local/share/tandem/data/" in uri:
                    host_path = uri.replace(
                        "/home/node/.local/share/tandem/data/",
                        "/home/evan/hal900/tandem-engine-state/data/",
                    )
                    print(f"  likely host path: {host_path}")

        if deep:
            print("\nTaking snapshot...")
            try:
                snap = sdk_browser_snapshot(cfg, session_id, include_screenshot=False)
                print(f"  snapshot: {str(snap)[:300]}")
            except Exception as e:
                print(f"  snapshot warning: {e}")

            print("\nExtracting HTML...")
            try:
                html_result = sdk_browser_extract(cfg, session_id, format="html")
                print(f"  extract: {str(html_result)[:300]}")
            except Exception as e:
                print(f"  extract warning: {e}")

        print("\nClosing session...")
        sdk_browser_close(cfg, session_id)
        print("  closed")
        return True
    except Exception as e:
        print(f"\nBrowser test FAILED for {url}: {e}")
        if session_id:
            try:
                sdk_browser_close(cfg, session_id)
            except Exception:
                pass
        return False


def test_browser(urls: list[str], deep: bool = False) -> int:
    print("Testing Tandem Browser tools via Python SDK...\n")

    root = Path(".")
    try:
        cfg = resolve_config(root)
    except Exception as e:
        print(f"Error resolving config: {e}")
        return 1

    status = check_browser_status(cfg)
    if not status or not status.enabled:
        print("\nBrowser is not enabled. Set ACA_BROWSER_ENABLED=true in .env and rebuild.")
        return 1

    if not status.browser.found:
        print("\nNo Chromium binary found. Install Chromium.")
        return 1

    if not status.runnable:
        issues = [i.code for i in (status.blocking_issues or [])]
        print(f"\nBrowser is enabled but not runnable. Blocking issues: {issues}")
        return 1

    ok = True
    for raw in urls:
        url = normalize_url(raw)
        if not url:
            continue
        ok = run_one_url(cfg, url, deep=deep) and ok

    if ok:
        print("\nBrowser test PASSED")
        return 0
    print("\nBrowser test completed with failures")
    return 1


test_browser.__test__ = False


if __name__ == "__main__":
    args = parse_args()
    targets = [*args.urls, *args.urls_flag]
    if not targets:
        targets = ["https://frumu.ai"]
    raise SystemExit(test_browser(targets, deep=args.deep))
