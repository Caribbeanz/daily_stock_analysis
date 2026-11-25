import type React from 'react';
import { Link } from 'react-router-dom';

/**
 * 助手独立页面的外壳（**不套 `<Shell>`**，与 `/login` 同构，方案 §6.1）。
 *
 * 导航是单向的：这里可以跳回现有工程页面，但现有页面不加反向入口——
 * 免得在没完成验收前让助手入口出现在每个用户的菜单里。
 */
export const Layout: React.FC<{
  title: string;
  description?: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
}> = ({ title, description, actions, children }) => (
  <div className="min-h-screen bg-base">
    <header className="border-b border-border/60 bg-card/70 backdrop-blur">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-3 px-4 py-4 md:flex-row md:items-center md:justify-between md:px-6">
        <div>
          <span className="label-uppercase">股票分析助手</span>
          <h1 className="mt-1 text-xl font-semibold text-foreground md:text-2xl">{title}</h1>
          {description ? <p className="mt-1 text-xs text-secondary-text">{description}</p> : null}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {actions}
          <Link
            to="/"
            className="rounded-lg border border-border/60 px-3 py-1.5 text-xs text-secondary-text hover:text-foreground"
          >
            返回工作台
          </Link>
        </div>
      </div>
    </header>
    <main className="mx-auto w-full max-w-6xl px-4 py-5 md:px-6 md:py-6">{children}</main>
  </div>
);
