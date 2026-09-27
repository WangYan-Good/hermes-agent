/** First /chat visit mounts the Native host; later route changes only hide it. */
export function latchChatActivation(previous: boolean, isActive: boolean): boolean {
  return previous || isActive;
}

export function chatHostDisposition(embedded: boolean, overridden: boolean, confirmed: boolean, loading: boolean, activated: boolean): 'suppressed' | 'waiting' | 'mounted' | 'inactive' {
  if (!embedded || overridden) return 'suppressed';
  if (!confirmed || loading) return 'waiting';
  return activated ? 'mounted' : 'inactive';
}
