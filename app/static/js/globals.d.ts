// 全局环境声明（浏览器端全局变量）
export {};
declare global {
  interface Window {
    /** 调试/自动化钩子（scripts/browser_test.js 契约） */
    __vc: any;
  }
}
