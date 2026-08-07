/** Build the ``session.create`` params for the dashboard chat sidecar. */
export function sidecarSessionCreateParams(
  profile?: string,
): Record<string, unknown> {
  return {
    close_on_disconnect: true,
    source: "tool",
    ...(profile ? { profile } : {}),
  };
}
