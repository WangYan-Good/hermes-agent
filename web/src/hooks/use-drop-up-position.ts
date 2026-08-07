import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useState,
  type RefObject,
} from "react";

export interface DropUpPosition {
  bottom: number;
  left: number;
}

export function useDropUpPosition(
  anchorRef: RefObject<HTMLElement | null>,
  enabled: boolean,
): DropUpPosition | null {
  const [position, setPosition] = useState<DropUpPosition | null>(null);
  const measure = useCallback(() => {
    const rect = anchorRef.current?.getBoundingClientRect();
    const next = rect
      ? { bottom: window.innerHeight - rect.top + 4, left: rect.left }
      : null;
    setPosition((current) =>
      current?.bottom === next?.bottom && current?.left === next?.left
        ? current
        : next,
    );
  }, [anchorRef]);

  // Measure after every open render. This covers responsive sheet→dropdown
  // transitions and parent layout changes that move the anchor without a
  // window resize event. The equality guard in measure prevents render loops.
  useLayoutEffect(() => {
    if (enabled) measure();
  });

  useEffect(() => {
    if (!enabled) return;

    const anchor = anchorRef.current;
    const observer = new ResizeObserver(measure);
    if (anchor) observer.observe(anchor);
    window.addEventListener("resize", measure);
    window.addEventListener("scroll", measure, true);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", measure);
      window.removeEventListener("scroll", measure, true);
    };
  }, [anchorRef, enabled, measure]);

  return position;
}
