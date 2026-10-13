import type React from 'react';
import { useState } from 'react';
import { Briefcase, Menu, MessageSquare, Settings as SettingsIcon, Star } from 'lucide-react';
import { NavLink, Outlet, useLocation } from 'react-router-dom';
import { Drawer } from '../common/Drawer';
import { SidebarNav } from '../layout/SidebarNav';
import { ThemeToggle } from '../theme/ThemeToggle';
import { UiLanguageToggle } from '../i18n/UiLanguageToggle';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { cn } from '../../utils/cn';

type MobileShellProps = {
  children?: React.ReactNode;
};

type TabItem = {
  to: string;
  labelKey: 'mobile.nav.watchlist' | 'mobile.nav.portfolio' | 'mobile.nav.chat' | 'mobile.nav.settings';
  icon: React.ComponentType<{ className?: string }>;
  /** 是否在"路径以该值开头"时也视为选中（子路由高亮）。 */
  matchPrefix?: boolean;
};

const TABS: TabItem[] = [
  { to: '/', labelKey: 'mobile.nav.watchlist', icon: Star },
  { to: '/portfolio', labelKey: 'mobile.nav.portfolio', icon: Briefcase },
  { to: '/chat', labelKey: 'mobile.nav.chat', icon: MessageSquare },
  { to: '/settings', labelKey: 'mobile.nav.settings', icon: SettingsIcon },
];

/**
 * 移动端壳层（P2 · FR-14.0.4）。
 *
 * - 底部 Tab 四入口：自选 / 持仓 / 对话 / 设置（高频路径一屏可达）；
 * - 「更多」抽屉复用桌面端 `SidebarNav`，保证所有页面在手机端仍可达；
 * - 内容区预留底部安全区（`pb-24`），避免被 Tab 栏遮挡；
 * - 桌面端由 `Shell` 自动切换回侧边栏布局，两端同一套路由。
 */
export const MobileShell: React.FC<MobileShellProps> = ({ children }) => {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const { t } = useUiLanguage();
  const location = useLocation();

  const isActive = (tab: TabItem): boolean => {
    if (tab.to === '/') {
      const matched = location.pathname === '/' || location.pathname.startsWith('/m/');
      return matched;
    }
    return location.pathname === tab.to || location.pathname.startsWith(`${tab.to}/`);
  };

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="sticky top-0 z-30 flex items-center justify-between gap-2 border-b border-border/70 bg-card/85 px-3 py-2 backdrop-blur-md">
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-foreground">
            {t('mobile.shell.title')}
          </p>
          <p className="truncate text-[11px] text-secondary-text">{t('mobile.shell.subtitle')}</p>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <UiLanguageToggle />
          <ThemeToggle />
        </div>
      </header>

      <main className="mx-auto w-full max-w-2xl px-3 pb-24 pt-3 touch-pan-y">
        {children ?? <Outlet />}
      </main>

      <nav
        className="fixed inset-x-0 bottom-0 z-40 border-t border-border/70 bg-card/95 pb-[env(safe-area-inset-bottom)] backdrop-blur-md"
        aria-label={t('mobile.nav.aria')}
      >
        <ul className="mx-auto flex w-full max-w-2xl items-stretch">
          {TABS.map((tab) => {
            const Icon = tab.icon;
            const active = isActive(tab);
            return (
              <li key={tab.to} className="flex-1">
                <NavLink
                  to={tab.to}
                  className={cn(
                    'flex flex-col items-center gap-0.5 px-1 py-2 text-[11px] transition-colors',
                    active ? 'text-primary' : 'text-secondary-text hover:text-foreground',
                  )}
                  aria-current={active ? 'page' : undefined}
                >
                  <Icon className="h-5 w-5" />
                  <span className="truncate">{t(tab.labelKey)}</span>
                </NavLink>
              </li>
            );
          })}
          <li className="flex-1">
            <button
              type="button"
              onClick={() => setDrawerOpen(true)}
              className="flex w-full flex-col items-center gap-0.5 px-1 py-2 text-[11px] text-secondary-text transition-colors hover:text-foreground"
            >
              <Menu className="h-5 w-5" />
              <span className="truncate">{t('mobile.nav.more')}</span>
            </button>
          </li>
        </ul>
      </nav>

      <Drawer
        isOpen={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        title={t('layout.navMenu')}
        width="max-w-xs"
        zIndex={90}
        side="left"
      >
        <SidebarNav onNavigate={() => setDrawerOpen(false)} />
      </Drawer>
    </div>
  );
};
