import { defineRailway, github, postgres, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  const Postgres = postgres("Postgres", { region: "sfo" });
  Postgres.networking = { privateNetworkEndpoint: "postgres" };
  const postgresVolume = volume("postgres-volume", { alerts: { usage: { "100": {}, "80": {}, "95": {} } }, allowOnlineResize: true, region: "sfo", sizeMB: 10240 });
  const opengridTerminal = service("opengrid-terminal", {
    source: github("fzsheik/opengrid-terminal", { branch: "main" }),
    start: "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1",
    healthcheck: "/health",
    healthcheckTimeout: 60,
    replicas: { "sfo": 1 },
    domains: ["data.tryopengrid.com", "terminal.tryopengrid.com"],
  });
  const probeAgentEuWest = service("probe-agent-eu-west", {
    start: "python -m probes.agent",
    replicas: { "europe-west4-drams3a": 1 },
  });
  const probeAgentSoutheastAsia = service("probe-agent-southeast-asia", {
    start: "python -m probes.agent",
    replicas: { "asia-southeast1-eqsg3a": 1 },
  });

  return project("pure-gentleness", {
    variables: { managed: false },
    resources: [Postgres, opengridTerminal, probeAgentEuWest, probeAgentSoutheastAsia, postgresVolume],
  });
});
