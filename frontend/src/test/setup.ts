import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

// React 18 needs this flag when act() is used outside React Testing Library.
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });

// jsdom 不支持伪元素样式查询，rc-table 量测滚动条时会产生 Not implemented 噪音
const nativeGetComputedStyle = window.getComputedStyle.bind(window);
window.getComputedStyle = (element: Element, pseudoElement?: string | null) =>
  nativeGetComputedStyle(element, pseudoElement === '::-webkit-scrollbar' ? undefined : pseudoElement);

// antd 的响应式栅格/Table 会读取 matchMedia，jsdom 默认没有实现
if (!window.matchMedia) {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => undefined,
      removeListener: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      dispatchEvent: () => false,
    }),
  });
}

afterEach(() => {
  cleanup();
});
