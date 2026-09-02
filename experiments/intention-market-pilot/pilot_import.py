"""Create placeholder accounts and import their Community Archive tweets
through the app's own pre-fill path (parquet snapshot, replies kept, no
profile seeding — the lab's celery-beat is disabled, so nothing downstream
spends money). Idempotent: accounts already imported are skipped."""
import argparse
import time
import traceback

from pilot_common import OUT, app, accounts, append_jsonl, user_for


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--count", type=int, default=16)
    ap.add_argument("--max-failures", type=int, default=3)
    args = ap.parse_args()

    flask_app = app()
    with flask_app.app_context():
        from backend.extensions import db
        from backend.models import User
        from backend.tasks.imports import prefill_community_archive_impl

        failures = 0
        for r in accounts()[args.start:args.start + args.count]:
            h = r["username"]
            if user_for(h):
                print(f"  {h:<18} already imported, skipping", flush=True)
                continue
            user = User(username=f"ca_{h}"[:64], email=f"{h}@prefill.lab.invalid"[:128],
                        plan="alpha")
            db.session.add(user)
            db.session.commit()
            t0 = time.time()
            try:
                res = prefill_community_archive_impl(
                    user.id, h, {"include_replies": True, "force_parquet": True},
                    seed_now=False)
                res = {k: v for k, v in res.items()
                       if isinstance(v, (int, float, str, bool, type(None)))}
                res.update({"seconds": round(time.time() - t0, 1), "ok": True})
            except Exception as e:  # noqa: BLE001 — record and continue
                db.session.rollback()
                res = {"user_id": user.id, "handle": h, "ok": False,
                       "error": repr(e)[:300], "seconds": round(time.time() - t0, 1)}
                failures += 1
                traceback.print_exc()
            append_jsonl(OUT / "imports.jsonl", res)
            print(f"  {h:<18} ok={res['ok']} nodes={res.get('total')} "
                  f"tokens={res.get('imported_tokens')} src={res.get('source')} "
                  f"{res['seconds']}s", flush=True)
            if failures > args.max_failures:
                print("too many failures — stopping (stop condition)")
                break


if __name__ == "__main__":
    main()
