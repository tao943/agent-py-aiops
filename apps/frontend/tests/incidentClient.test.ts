import { describe, expect, it, vi } from "vitest";

import { createIncidentClient } from "../src/incidents/incidentClient";

describe("Incident client", () => {
  it("uses typed owner-scoped list and diagnose endpoints", async () => {
    const fetchImpl = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const path = String(input);
      const data = path.includes(":diagnose")
        ? { incidentId: "incident_1", diagnosticTaskId: "diagnostic_1", backgroundJobId: "job_1", reused: false }
        : { items: [], nextCursor: null };
      return new Response(JSON.stringify({ ok: true, data, meta: { requestId: "req_1" } }), {
        status: path.includes(":diagnose") ? 202 : 200,
        headers: { "Content-Type": "application/json" }
      });
    });
    const client = createIncidentClient({
      baseUrl: "http://api.test",
      fetchImpl: fetchImpl as typeof fetch,
      getAccessToken: () => "token"
    });

    await client.listIncidents({ status: "all", limit: 25, cursor: "opaque" });
    await client.diagnoseIncident("incident_1", { note: "复核连接池" });

    expect(fetchImpl.mock.calls[0]?.[0]).toBe(
      "http://api.test/aiops/incidents?status=all&limit=25&cursor=opaque"
    );
    expect(fetchImpl.mock.calls[1]?.[0]).toBe(
      "http://api.test/aiops/incidents/incident_1:diagnose"
    );
    expect(fetchImpl.mock.calls[1]?.[1]).toMatchObject({ method: "POST" });
    expect(JSON.parse(String(fetchImpl.mock.calls[1]?.[1]?.body))).toEqual({ note: "复核连接池" });
  });
});
