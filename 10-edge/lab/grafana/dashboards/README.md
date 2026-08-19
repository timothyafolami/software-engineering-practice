# Dashboards

Deliberately empty. Topic 2's run 3 is *building* the misleading dashboard
yourself and screenshotting it twice — first with only throughput and GPU
utilisation on the panel, then with queue depth and TTFT p99 added — and a
dashboard shipped here would hand you the punchline before the experiment.

Grafana is provisioned to load any `*.json` dropped in this directory
(see `../provisioning/dashboards/dashboards.yml`), so export yours here
once you have built it and it will survive `docker compose down`.

The Prometheus datasource is provisioned already, at `http://prom:9090`.
