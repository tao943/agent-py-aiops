import { describe, expect, it, vi } from "vitest";

import { createSseClient } from "../src/api/sseClient";

describe("SSE client replay", () => {
  it("passes Last-Event-ID and trusts the SSE id field", async () => {
    const fetchImpl = vi.fn(async () => new Response(
      'id: 7\nevent: run.status\ndata: {"id":"wrong","type":"run.status","channel":"chat","timestamp":"2026-08-22T00:00:00Z","run":{"id":"r1","status":"running"}}\n\n',
      { status: 200, headers: { "Content-Type": "text/event-stream" } }
    ));
    const client = createSseClient({
      baseUrl: "http://test",
      fetchImpl: fetchImpl as typeof fetch,
      getAccessToken: () => "token"
    });
    const events = [];
    for await (const event of client.stream("/events", {}, { lastEventId: 6 })) events.push(event);

    expect(fetchImpl).toHaveBeenCalledWith("http://test/events", expect.objectContaining({
      headers: expect.objectContaining({ "Last-Event-ID": "6" })
    }));
    expect(events[0]?.id).toBe("7");
  });
});
