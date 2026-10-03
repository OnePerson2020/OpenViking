import * as React from 'react'
import { Link, useNavigate, useRouterState } from '@tanstack/react-router'
import {
  MessagesSquareIcon,
  BookOpenIcon,
  BracesIcon,
  BrainIcon,
  ChevronRightIcon,
  ListChecksIcon,
  HistoryIcon,
  HomeIcon,
  FolderIcon,
  GithubIcon,
  KeyRoundIcon,
  MoonIcon,
  ActivityIcon,
  BotIcon,
  PanelsTopLeftIcon,
  CableIcon,
  ScrollTextIcon,
  SearchIcon,
  SparklesIcon,
  SunIcon,
  UsersRoundIcon,
  WorkflowIcon,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useTheme } from 'next-themes'

import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '#/components/ui/collapsible'
import { CrossDeviceVerifyDialog } from '#/components/cross-device-verify-dialog'
import { AccountSwitcher } from '#/components/account-switcher'
import { CurrentUserMenu } from '#/components/current-user-menu'
import { GeneratedCredentialDialog } from '#/components/generated-credential-dialog'
import { ScrollArea } from '#/components/ui/scroll-area'
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSub,
  SidebarMenuSubButton,
  SidebarMenuSubItem,
  SidebarProvider,
  SidebarTrigger,
} from '#/components/ui/sidebar'
import {
  AppConnectionProvider,
  useAppConnection,
} from '#/hooks/use-app-connection'
import type { ServerMode } from '#/hooks/use-server-mode'
import { cn } from '#/lib/utils'
import { useStudioCapabilities } from '#/hooks/use-studio-capabilities'
import { useStudioService } from '#/hooks/use-studio-service'
import { featureForPath, isStudioFeatureVisible } from '#/lib/studio-service'
import { resolveDomainDestination } from '#/lib/studio-navigation'
import type { StudioDomain } from '#/lib/studio-service'
import { StudioFeatureBoundary } from './studio-feature-boundary'
import { resolveStudioManagementCapabilities } from '#/lib/studio-permissions'

type NavItem = {
  icon: React.ComponentType
  id: string
  titleKey: string
  to: string
  children?: readonly NavSubItem[]
}

type NavSubItem = {
  icon: React.ComponentType
  id: string
  titleKey: string
  to: string
}

type NavGroupItemProps = {
  item: NavItem & { children: readonly NavSubItem[] }
  pathname: string
  title: string
  t: ReturnType<typeof useTranslation>['t']
}

const NAV_ITEMS: readonly NavItem[] = [
  {
    icon: FolderIcon,
    id: 'directory',
    titleKey: 'studio:directory',
    to: '/directory',
  },
  {
    icon: UsersRoundIcon,
    id: 'users',
    titleKey: 'footer.users',
    to: '/users',
  },
  {
    icon: HomeIcon,
    id: 'home',
    titleKey: 'studio:statistics',
    to: '/home',
  },
  {
    icon: PanelsTopLeftIcon,
    id: 'playground',
    titleKey: 'navigation.playground.title',
    to: '/playground',
  },
  {
    icon: BotIcon,
    id: 'vikingbot',
    titleKey: 'vikingbot:title',
    to: '/vikingbot',
  },
  {
    icon: SearchIcon,
    id: 'retrieval',
    titleKey: 'navigation.retrieval.title',
    to: '/retrieval',
  },
  {
    icon: SparklesIcon,
    id: 'skills',
    titleKey: 'navigation.skills.title',
    to: '/skills',
  },

  {
    icon: WorkflowIcon,
    id: 'compile',
    titleKey: 'navigation.compile.title',
    to: '/compile',
  },
  {
    icon: MessagesSquareIcon,
    id: 'sessions',
    titleKey: 'studio:sessionRecords',
    to: '/sessions',
  },
  {
    icon: BrainIcon,
    id: 'agentExperience',
    titleKey: 'navigation.agentExperience.title',
    to: '/agent-experience',
  },
  {
    icon: ScrollTextIcon,
    id: 'requestLogs',
    titleKey: 'navigation.requestLogs.title',
    to: '/request-logs',
  },
  {
    icon: ListChecksIcon,
    id: 'tasks',
    titleKey: 'studio:processingTasks',
    to: '/tasks',
  },
  {
    icon: HistoryIcon,
    id: 'watches',
    titleKey: 'navigation.watches.title',
    to: '/watches',
  },
  {
    icon: ActivityIcon,
    id: 'monitoring',
    titleKey: 'navigation.monitoring.title',
    to: '/monitoring',
  },
] as const

const NAV_SECTIONS = [
  { id: 'workspace', titleKey: 'studio:domains.workspace' },
  { id: 'extensions', titleKey: 'studio:domains.extensions' },
  { id: 'management', titleKey: 'studio:domains.management' },
] as const

const LANGUAGE_OPTIONS = [
  {
    shortLabel: '中',
    title: '中文',
    value: 'zh-CN',
  },
  {
    shortLabel: 'EN',
    title: 'English',
    value: 'en',
  },
] as const

const HEADER_ICON_BUTTON_CLASS =
  'relative inline-flex h-8 w-8 items-center justify-center rounded-lg border border-border/80 bg-muted/60 text-muted-foreground shadow-xs transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2'

function resolveLanguage(
  value: string | undefined,
): (typeof LANGUAGE_OPTIONS)[number]['value'] {
  if (value?.toLowerCase().startsWith('zh')) {
    return 'zh-CN'
  }

  return 'en'
}

function NavGroupItem({ item, pathname, title, t }: NavGroupItemProps) {
  const Icon = item.icon
  const isActive = pathname === item.to || pathname.startsWith(`${item.to}/`)
  const [open, setOpen] = React.useState(isActive)

  React.useEffect(() => {
    if (isActive) {
      setOpen(true)
    }
  }, [isActive])

  return (
    <Collapsible
      open={open}
      onOpenChange={setOpen}
      className="group/collapsible"
    >
      <SidebarMenuItem>
        <CollapsibleTrigger
          render={
            <SidebarMenuButton tooltip={title}>
              <Icon />
              <span>{title}</span>
              <ChevronRightIcon className="ml-auto transition-transform duration-200 group-data-[open]/collapsible:rotate-90" />
            </SidebarMenuButton>
          }
        />
        <CollapsibleContent>
          <SidebarMenuSub>
            {item.children.map((child) => {
              const ChildIcon = child.icon
              const childActive =
                pathname === child.to ||
                (child.to !== item.to && pathname.startsWith(`${child.to}/`))
              const childTitle = t(child.titleKey, { ns: 'appShell' })

              return (
                <SidebarMenuSubItem key={child.id}>
                  <SidebarMenuSubButton
                    render={<Link to={child.to} />}
                    isActive={childActive}
                  >
                    <ChildIcon />
                    <span>{childTitle}</span>
                  </SidebarMenuSubButton>
                </SidebarMenuSubItem>
              )
            })}
          </SidebarMenuSub>
        </CollapsibleContent>
      </SidebarMenuItem>
    </Collapsible>
  )
}

export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <AppConnectionProvider>
      <IdentityScopedAppShell>{children}</IdentityScopedAppShell>
    </AppConnectionProvider>
  )
}

function IdentityScopedAppShell({ children }: { children: React.ReactNode }) {
  const { identityScopeKey, serverMode } = useAppConnection()
  return (
    <AppShellInner key={identityScopeKey}>
      <ConnectionScopedRouteContent serverMode={serverMode}>
        <StudioFeatureBoundary>{children}</StudioFeatureBoundary>
      </ConnectionScopedRouteContent>
    </AppShellInner>
  )
}

export function ConnectionScopedRouteContent({
  children,
  serverMode,
}: {
  children: React.ReactNode
  serverMode: ServerMode
}) {
  return serverMode === 'checking' ? null : children
}

function AppShellInner({ children }: { children: React.ReactNode }) {
  const { i18n, t } = useTranslation(['appShell', 'common'])
  const navigate = useNavigate()
  const pathname = useRouterState({
    select: (state) => state.location.pathname,
  })
  const { setTheme, resolvedTheme } = useTheme()
  const currentLanguage = resolveLanguage(
    i18n.resolvedLanguage ?? i18n.language,
  )
  const agentIntegrationsHref = `https://docs.openviking.ai/${
    currentLanguage === 'zh-CN' ? 'zh' : 'en'
  }/agent-integrations/01-overview`
  const opensourceApiHref = `https://docs.openviking.ai/${
    currentLanguage === 'zh-CN' ? 'zh' : 'en'
  }/api/01-overview`
  const [crossDeviceVerifyOpen, setCrossDeviceVerifyOpen] =
    React.useState(false)
  const { connection, connectionRole, isConnectionRoleLoading, serverMode } =
    useAppConnection()
  const settingsActive = pathname === '/settings'
  const { provider } = useStudioService()
  const sdkApiHref =
    provider === 'volcengine'
      ? 'https://docs.volcengine.com/docs/84313/2381937'
      : opensourceApiHref
  const { canManageUsers } = resolveStudioManagementCapabilities({
    serviceProvider: provider,
    hasControlCredential: Boolean(connection.adminApiKey.trim()),
    isRoleLoading: isConnectionRoleLoading,
    role: connectionRole,
    serverMode,
  })
  const crossDeviceVerifyActive =
    pathname === '/oauth/verify' || pathname.startsWith('/oauth/verify/')
  const service = useStudioService()
  const activeDomain = featureForPath(pathname)?.domain ?? 'workspace'
  const capabilities = useStudioCapabilities()
  const availableNavItems = NAV_ITEMS.filter((item) => {
    const feature = featureForPath(item.to)
    if (!feature) return false
    if (feature.domain === 'management' && !canManageUsers) return false
    return isStudioFeatureVisible(
      provider,
      feature,
      capabilities[feature.id]?.data,
    )
  })
  const visibleNavItems = availableNavItems.filter(
    (item) => featureForPath(item.to)?.domain === activeDomain,
  )
  const extensionEntry = availableNavItems.find(
    (item) => featureForPath(item.to)?.domain === 'extensions',
  )

  const navigationPaths = availableNavItems.map((item) => item.to)
  if (navigationPaths.includes('/directory')) navigationPaths.push('/memories')
  const domainPages = React.useRef<Partial<Record<StudioDomain, string>>>({})
  React.useEffect(() => {
    const feature = featureForPath(pathname)
    if (feature && navigationPaths.includes(feature.to)) {
      domainPages.current[feature.domain] = pathname
    }
  }, [pathname, availableNavItems])
  const availableDomains = NAV_SECTIONS.filter(
    (section) =>
      (section.id !== 'management' || canManageUsers) &&
      (section.id !== 'extensions' || Boolean(extensionEntry)),
  )

  function openCrossDeviceVerify(): void {
    if (crossDeviceVerifyActive) {
      return
    }
    // Desktop: open the dialog so the user keeps the current page underneath.
    // Phone / narrow tablets: navigate to the dedicated page since fullscreen
    // dialogs are awkward to dismiss on mobile.
    const useDialog =
      typeof window !== 'undefined' &&
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(min-width: 768px)').matches
    if (useDialog) {
      setCrossDeviceVerifyOpen(true)
    } else {
      void navigate({ to: '/oauth/verify' })
    }
  }

  return (
    <SidebarProvider
      defaultOpen
      className="flex h-svh overflow-hidden bg-sidebar"
    >
      <Sidebar variant="sidebar" collapsible="icon">
        <SidebarHeader className="h-12 border-b border-sidebar-border/70 px-2 py-0">
          <div className="flex h-full items-center justify-between gap-2 group-data-[collapsible=icon]:justify-center">
            <div className="min-w-0 flex-1 group-data-[collapsible=icon]:hidden">
              <AccountSwitcher />
            </div>
            <SidebarTrigger className="hidden shrink-0 md:inline-flex" />
          </div>
        </SidebarHeader>

        <SidebarContent className="gap-0 py-1">
          {NAV_SECTIONS.filter(
            (section) =>
              section.id === activeDomain && visibleNavItems.length > 0,
          ).map((section) => (
            <SidebarGroup key={section.id} className="pb-1">
              <SidebarGroupLabel className="h-7 px-2 pt-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-sidebar-foreground/45">
                {t(section.titleKey, { ns: 'appShell' })}
              </SidebarGroupLabel>
              <SidebarGroupContent>
                <SidebarMenu>
                  {visibleNavItems
                    .filter(
                      (item) =>
                        (featureForPath(item.to)?.domain ?? 'workspace') ===
                        section.id,
                    )
                    .map((item) => {
                      const isActive =
                        pathname === item.to ||
                        pathname.startsWith(`${item.to}/`)
                      const title = t(item.titleKey, { ns: 'appShell' })

                      if (item.children) {
                        return (
                          <NavGroupItem
                            key={item.id}
                            item={
                              item as NavItem & {
                                children: readonly NavSubItem[]
                              }
                            }
                            pathname={pathname}
                            title={title}
                            t={t}
                          />
                        )
                      }

                      const Icon = item.icon

                      return (
                        <SidebarMenuItem key={item.id}>
                          <SidebarMenuButton
                            render={<Link to={item.to} />}
                            isActive={isActive}
                            tooltip={title}
                            className="h-9"
                          >
                            <Icon />
                            <span>{title}</span>
                          </SidebarMenuButton>
                        </SidebarMenuItem>
                      )
                    })}
                </SidebarMenu>
              </SidebarGroupContent>
            </SidebarGroup>
          ))}

          <SidebarGroup className="pb-1">
            <SidebarGroupLabel className="h-7 px-2 pt-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-sidebar-foreground/45">
              {t('sidebar.groups.settings', { ns: 'appShell' })}
            </SidebarGroupLabel>
            <SidebarGroupContent>
              <SidebarMenu>
                <SidebarMenuItem>
                  <SidebarMenuButton
                    render={<Link to="/settings" />}
                    isActive={settingsActive}
                    tooltip={t('footer.connection', { ns: 'appShell' })}
                    className="h-9"
                  >
                    <CableIcon />
                    <span>{t('footer.connection', { ns: 'appShell' })}</span>
                  </SidebarMenuButton>
                </SidebarMenuItem>
                {provider === 'opensource' ? (
                  <SidebarMenuItem>
                    <SidebarMenuButton
                      onClick={openCrossDeviceVerify}
                      isActive={crossDeviceVerifyActive}
                      tooltip={t('navigation.crossDeviceVerify.title', {
                        ns: 'appShell',
                      })}
                      className="h-9"
                    >
                      <KeyRoundIcon />
                      <span>
                        {t('navigation.crossDeviceVerify.title', {
                          ns: 'appShell',
                        })}
                      </span>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ) : null}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>

          <SidebarGroup className="pb-1">
            <SidebarGroupLabel className="h-7 px-2 pt-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-sidebar-foreground/45">
              {t('sidebar.groups.resources', { ns: 'appShell' })}
            </SidebarGroupLabel>
            <SidebarGroupContent>
              <SidebarMenu>
                <SidebarMenuItem>
                  <SidebarMenuButton
                    render={
                      <a
                        href={
                          provider === 'volcengine'
                            ? sdkApiHref
                            : 'https://docs.openviking.ai/'
                        }
                        target="_blank"
                        rel="noreferrer"
                      />
                    }
                    tooltip={t('footer.docs', { ns: 'appShell' })}
                    className="h-9"
                  >
                    <BookOpenIcon />
                    <span>{t('footer.docs', { ns: 'appShell' })}</span>
                  </SidebarMenuButton>
                </SidebarMenuItem>
                <SidebarMenuItem>
                  <SidebarMenuButton
                    render={
                      <a href={sdkApiHref} target="_blank" rel="noreferrer" />
                    }
                    tooltip={t('footer.sdkApi', { ns: 'appShell' })}
                    className="h-9"
                  >
                    <BracesIcon />
                    <span>{t('footer.sdkApi', { ns: 'appShell' })}</span>
                  </SidebarMenuButton>
                </SidebarMenuItem>
                {provider === 'opensource' ? (
                  <SidebarMenuItem>
                    <SidebarMenuButton
                      render={
                        <a
                          href={agentIntegrationsHref}
                          target="_blank"
                          rel="noreferrer"
                        />
                      }
                      tooltip={t('footer.agentIntegrations', {
                        ns: 'appShell',
                      })}
                      className="h-9"
                    >
                      <BotIcon />
                      <span>
                        {t('footer.agentIntegrations', { ns: 'appShell' })}
                      </span>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ) : null}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        </SidebarContent>
      </Sidebar>

      <SidebarInset className="min-h-0 flex-1 overflow-hidden rounded-none border-0 bg-background shadow-none ring-0 md:m-0 md:ml-0">
        <header className="flex h-12 shrink-0 items-center justify-end border-b border-border/70 bg-sidebar px-4 backdrop-blur-md md:px-6">
          <SidebarTrigger className="mr-auto shrink-0 md:hidden" />
          <div className="flex items-center gap-2">
            <div
              aria-label={t('language.label', { ns: 'common' })}
              className="relative flex h-8 items-center rounded-lg border border-border/80 bg-muted/60 p-1 text-xs shadow-xs"
              role="group"
            >
              <span
                className={cn(
                  'absolute h-6 min-w-8 rounded-md bg-background shadow-sm transition-transform duration-200 ease-in-out',
                  currentLanguage === 'en' && 'translate-x-full',
                )}
              />
              {LANGUAGE_OPTIONS.map((item) => {
                const isActive = item.value === currentLanguage

                return (
                  <button
                    key={item.value}
                    type="button"
                    aria-pressed={isActive}
                    className={cn(
                      'relative z-10 h-6 min-w-8 rounded-md px-2 text-xs font-semibold text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2',
                      isActive && 'text-foreground',
                    )}
                    onClick={() => {
                      if (!isActive) {
                        void i18n.changeLanguage(item.value)
                      }
                    }}
                  >
                    {item.shortLabel}
                  </button>
                )
              })}
            </div>

            <button
              type="button"
              aria-label={t('theme.toggle', { ns: 'common' })}
              className={HEADER_ICON_BUTTON_CLASS}
              onClick={() =>
                setTheme(resolvedTheme === 'dark' ? 'light' : 'dark')
              }
            >
              <MoonIcon className="size-4 rotate-0 scale-100 transition-all dark:-rotate-90 dark:scale-0" />
              <SunIcon className="absolute size-4 rotate-90 scale-0 transition-all dark:rotate-0 dark:scale-100" />
            </button>

            <a
              href="https://github.com/volcengine/OpenViking"
              target="_blank"
              rel="noreferrer"
              aria-label={t('footer.github', { ns: 'appShell' })}
              className={HEADER_ICON_BUTTON_CLASS}
            >
              <GithubIcon className="size-4" />
            </a>

            <div className="h-6 w-px bg-border/80" aria-hidden="true" />

            <CurrentUserMenu />
          </div>
        </header>

        <nav
          aria-label={t('studio:domainsLabel')}
          className="flex shrink-0 flex-wrap items-center gap-2 border-b px-4 py-2 md:px-6"
        >
          {(availableDomains.length > 1 ? availableDomains : []).map(
            (section) => (
              <Link
                key={section.id}
                to={resolveDomainDestination(
                  section.id,
                  featureForPath(pathname)?.domain === section.id
                    ? pathname
                    : domainPages.current[section.id],
                  navigationPaths,
                )}
                aria-current={activeDomain === section.id ? 'page' : undefined}
                className={cn(
                  'rounded-md px-3 py-2 text-sm text-muted-foreground hover:bg-muted',
                  activeDomain === section.id &&
                    'bg-primary/10 font-medium text-primary',
                )}
              >
                {t(section.titleKey)}
              </Link>
            ),
          )}
          <span className="ml-auto text-xs text-muted-foreground">
            {t(
              `studio:providers.${service.isChecking ? 'checking' : service.provider}`,
            )}{' '}
            {service.version}
          </span>
        </nav>
        {pathname === '/playground' ||
        pathname === '/vikingbot' ||
        pathname.startsWith('/vikingbot/') ||
        pathname === '/sessions' ||
        pathname.startsWith('/sessions/') ? (
          <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
            {children}
          </div>
        ) : (
          <ScrollArea className="min-h-0 flex-1">
            <div className="flex w-full flex-col gap-6 px-4 py-6 md:px-6">
              {children}
            </div>
          </ScrollArea>
        )}
      </SidebarInset>

      {provider === 'opensource' ? (
        <CrossDeviceVerifyDialog
          open={crossDeviceVerifyOpen}
          onOpenChange={setCrossDeviceVerifyOpen}
        />
      ) : null}
      <GeneratedCredentialDialog />
    </SidebarProvider>
  )
}
