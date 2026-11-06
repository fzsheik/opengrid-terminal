"""Run the app from the current directory on a seeded scratch database (no jobs, no lifespan). Args: port db_name."""
import os, sys
os.environ["OPENGRID_NO_JOBS"] = "1"
sys.path[:0] = [os.getcwd(), os.path.join(os.getcwd(), "tests")]
import scratchdb, fixtures, normalize  # scratchdb blocks real network
import uvicorn
port, db = int(sys.argv[1]), sys.argv[2]
normalize.SessionLocal = fixtures.session(scratchdb.create(db))
fixtures.seed(normalize.SessionLocal, days=10, providers=["lambda", "runpod", "vast", "coreweave", "nebius"])  # real names: provider pages resolve
from analytics import rollups
rollups.refresh()
import main
print("READY", flush=True)
try:
    uvicorn.run(main.app, host="127.0.0.1", port=port, lifespan="off", log_level="warning")
finally:
    normalize.SessionLocal.kw["bind"].dispose(); scratchdb.drop(db)
