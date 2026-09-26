/** First /chat visit mounts the Native host; later route changes only hide it. */
export function latchChatActivation(previous: boolean, isActive: boolean): boolean {
  return previous || isActive;
}
