import { useEffect, useState } from 'react';

/**
 * 响应式媒体查询 hook（P2 · FR-14.0.4）。
 *
 * - 首次渲染即给出同步结果（`window.matchMedia` 直接求值），避免移动端"闪一下桌面布局"；
 * - SSR / 无 `matchMedia` 环境下退化为 `false`；
 * - 监听查询变化（旋转屏幕、拖拽窗口）实时更新。
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState<boolean>(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
      return false;
    }
    return window.matchMedia(query).matches;
  });

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
      return undefined;
    }

    const mediaQueryList = window.matchMedia(query);
    const handleChange = (event: MediaQueryListEvent) => setMatches(event.matches);

    setMatches(mediaQueryList.matches);

    if (typeof mediaQueryList.addEventListener === 'function') {
      mediaQueryList.addEventListener('change', handleChange);
      return () => mediaQueryList.removeEventListener('change', handleChange);
    }

    // 老式浏览器兜底
    mediaQueryList.addListener(handleChange);
    return () => mediaQueryList.removeListener(handleChange);
  }, [query]);

  return matches;
}

/** 移动端断点与既有 Tailwind `lg`（1024px）保持一致。 */
export const MOBILE_QUERY = '(max-width: 1023px)';

export function useIsMobile(): boolean {
  return useMediaQuery(MOBILE_QUERY);
}
