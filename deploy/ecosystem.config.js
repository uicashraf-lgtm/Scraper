// PM2 ecosystem file for the PeptiPrices backend.
//
// Usage on the VPS (from the repo root):
//
//   pm2 start deploy/ecosystem.config.js
//   pm2 save
//
// After this, `pm2 list` will show `peptiprices-api` alongside NodeBB.
//
// `run.py` already spawns and supervises the scraper worker in-process
// (see run.py::_spawn_worker / _worker_watchdog), so only one PM2 entry
// is needed for the API + worker pair.

module.exports = {
  apps: [
    {
      name: "peptiprices-api",
      cwd: "/var/www/peptiprices-backend",
      script: "run.py",
      interpreter: "/var/www/peptiprices-backend/.venv/bin/python",
      env: {
        PYTHONUNBUFFERED: "1",
      },
      autorestart: true,
      max_restarts: 10,
      restart_delay: 3000,
      out_file: "/var/log/peptiprices/api.out.log",
      error_file: "/var/log/peptiprices/api.err.log",
      merge_logs: true,
      time: true,
    },
  ],
};
