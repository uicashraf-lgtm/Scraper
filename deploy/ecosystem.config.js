// PM2 ecosystem file for the PeptiPrices backend.
//
// Usage on the VPS (from the repo root):
//
//   pm2 start deploy/ecosystem.config.js
//   pm2 save
//
// After this, `pm2 list` will show `peptiprices-api` alongside NodeBB.
//
// Path resolution: `cwd` is derived from this file's location so the
// ecosystem works no matter where the repo is checked out. The Python
// interpreter is picked up from `$PEPTI_PYTHON`, else `.venv/bin/python`
// next to the repo if it exists, else plain `python3` on PATH. Override
// by exporting `PEPTI_PYTHON=/path/to/python` before `pm2 start`.
//
// `run.py` already spawns and supervises the scraper worker in-process
// (see run.py::_spawn_worker / _worker_watchdog), so only one PM2 entry
// is needed for the API + worker pair.

const path = require("path");
const fs = require("fs");

const repoRoot = path.resolve(__dirname, "..");
const venvPython = path.join(repoRoot, ".venv", "bin", "python");

let interpreter = process.env.PEPTI_PYTHON;
if (!interpreter) {
  interpreter = fs.existsSync(venvPython) ? venvPython : "python3";
}

module.exports = {
  apps: [
    {
      name: "peptiprices-api",
      cwd: repoRoot,
      script: "run.py",
      interpreter: interpreter,
      env: {
        PYTHONUNBUFFERED: "1",
      },
      autorestart: true,
      max_restarts: 10,
      restart_delay: 3000,
      merge_logs: true,
      time: true,
    },
  ],
};
