import { resolveApiKeyAuth } from '#/lib/studio-auth'
import * as React from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useNavigate, useRouterState } from '@tanstack/react-router'

import i18n from '#/i18n'
import { identifyServiceProvider } from '#/lib/studio-service'
import type { ServiceSelection } from '#/lib/studio-service'
import { fetchAdminAccounts } from '#/lib/admin'
import { isOvClientError, ovClient } from '#/lib/ov-client'

import {
  resolveServerAuthMode,
  resetServerHealthCache,
  fetchServerHealth,
  normalizeBaseUrl,
} from './use-server-mode'
import type { ServerMode } from './use-server-mode'

export type ConnectionRole = 'admin' | 'root' | 'unknown' | 'user'

export type ConnectionDraft = {
  serviceSelection?: ServiceSelection
  accountId: string
  adminApiKey: string
  apiKey: string
  baseUrl: string
  userId: string
}

export type ConnectionIdentitySummary = {
  labelKey: string
  values?: {
    identity?: string
  }
}

export type GeneratedCredential = {
  accountId?: string
  apiKey: string
  userId?: string
}

type AppConnectionContextValue = {
  clearGeneratedCredential: () => void
  connection: ConnectionDraft
  connectionRole: ConnectionRole
  generatedCredential: GeneratedCredential | null
  identityScopeKey: string
  isConnectionRoleLoading: boolean
  openConnectionSettings: () => void
  reconnect: () => void
  serverHealth: Record<string, unknown> | null
  saveConnection: (next: ConnectionDraft) => void
  setGeneratedCredential: (credential: GeneratedCredential) => void
  serverMode: ServerMode
  switchIdentity: (identity: {
    accountId: string
    allowLegacyIdentityFallback?: boolean
    apiKey: string
    userId: string
  }) => Promise<void>
  switchManagementAccount: (accountId: string) => Promise<void>
}

const CONNECTION_STORAGE_KEY = 'ov_console_connection'
const AUTH_PROMPT_SUPPRESSION_MS = 10000

type ConnectionErrorKey =
  | 'credentialMismatch'
  | 'identitySwitchUnsupported'
  | 'rootRequired'

export function createConnectionError(key: ConnectionErrorKey): Error {
  return new Error(i18n.t(`connection:errors.${key}`))
}

const ENV_BASE_URL =
  typeof import.meta.env.VITE_OV_BASE_URL === 'string'
    ? import.meta.env.VITE_OV_BASE_URL.trim()
    : ''
const ENV_API_KEY =
  typeof import.meta.env.VITE_OV_API_KEY === 'string'
    ? import.meta.env.VITE_OV_API_KEY.trim()
    : ''
const ENV_ADMIN_API_KEY =
  typeof import.meta.env.VITE_OV_ADMIN_API_KEY === 'string'
    ? import.meta.env.VITE_OV_ADMIN_API_KEY.trim()
    : ''
const ENV_ACCOUNT =
  typeof import.meta.env.VITE_OV_ACCOUNT === 'string'
    ? import.meta.env.VITE_OV_ACCOUNT.trim()
    : ''
const ENV_USER =
  typeof import.meta.env.VITE_OV_USER === 'string'
    ? import.meta.env.VITE_OV_USER.trim()
    : ''

const DEFAULT_CONNECTION: ConnectionDraft = {
  accountId: ENV_ACCOUNT || 'default',
  adminApiKey: ENV_ADMIN_API_KEY,
  apiKey: ENV_API_KEY,
  baseUrl: ovClient.getOptions().baseUrl,
  userId: ENV_USER || 'default',
}

const AppConnectionContext =
  React.createContext<AppConnectionContextValue | null>(null)

function isBrowser(): boolean {
  return typeof window !== 'undefined'
}

function isConnectionRole(value: unknown): value is ConnectionRole {
  return (
    value === 'root' ||
    value === 'admin' ||
    value === 'user' ||
    value === 'unknown'
  )
}

function readStoredConnection(): Partial<ConnectionDraft> {
  if (!isBrowser()) {
    return {}
  }

  try {
    const raw = window.localStorage.getItem(CONNECTION_STORAGE_KEY)
    if (!raw) {
      return {}
    }
    const parsed: unknown = JSON.parse(raw)
    return typeof parsed === 'object' && parsed !== null
      ? (parsed as Partial<ConnectionDraft>)
      : {}
  } catch {
    return {}
  }
}

function persistConnection(connection: ConnectionDraft): void {
  if (!isBrowser()) {
    return
  }

  try {
    window.localStorage.setItem(
      CONNECTION_STORAGE_KEY,
      JSON.stringify(connection),
    )
  } catch {
    // Ignore localStorage failures in restricted environments.
  }
}

function normalizeConnectionDraft(
  connection: ConnectionDraft,
): ConnectionDraft {
  return {
    serviceSelection:
      connection.serviceSelection === 'opensource' ||
      connection.serviceSelection === 'volcengine'
        ? connection.serviceSelection
        : 'auto',
    accountId: connection.accountId.trim(),
    adminApiKey: connection.adminApiKey.trim(),
    apiKey: connection.apiKey.trim(),
    // Keep the URL as typed (whitespace trimmed only). Stripping the trailing
    // slash here ran on every keystroke and fought the input: typing the "//"
    // of "http://" kept collapsing back to "http:". Trailing slashes are
    // stripped where the URL is actually used instead (ovClient.setOptions,
    // fetchServerHealth, detectConnectionRole, and the admin client).
    baseUrl: connection.baseUrl.trim(),
    userId: connection.userId.trim(),
  }
}

function hashSecret(value: string): string {
  let hash = 0x811c9dc5
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index)
    hash = Math.imul(hash, 0x01000193)
  }
  return (hash >>> 0).toString(36)
}

export function createIdentityScopeKey(
  connection: ConnectionDraft,
  serverMode: ServerMode,
): string {
  const dataKey = connection.apiKey || connection.adminApiKey
  return [
    normalizeBaseUrl(connection.baseUrl),
    connection.serviceSelection ?? 'auto',
    serverMode,
    connection.accountId,
    connection.userId,
    dataKey ? hashSecret(dataKey) : 'none',
  ].join('\u0000')
}

export function createConnectionRoleProbeKey(
  connection: ConnectionDraft,
  serverMode: ServerMode,
): string {
  return [
    normalizeBaseUrl(connection.baseUrl),
    connection.serviceSelection ?? 'auto',
    serverMode,
    connection.accountId,
    connection.userId,
    connection.adminApiKey ? hashSecret(connection.adminApiKey) : 'none',
    connection.apiKey ? hashSecret(connection.apiKey) : 'none',
  ].join('\u0000')
}

function resolveIdentityField(
  envValue: string,
  storedValue: string | undefined,
  defaultValue: string,
): string {
  if (envValue) {
    return envValue
  }
  return storedValue || defaultValue
}

export function resolveInitialApiKey({
  defaultApiKey,
  envApiKey,
  storedApiKey,
}: {
  defaultApiKey: string
  envApiKey: string
  storedApiKey: string | undefined
}): string {
  return envApiKey || storedApiKey || defaultApiKey
}

export function resolveConnectionRoleProbeState({
  accountId = '',
  apiKey,
  baseUrl,
  serverMode,
  userId = '',
}: {
  accountId?: string
  apiKey: string
  baseUrl: string
  serverMode: ServerMode
  userId?: string
}): {
  isLoading: boolean
  role: ConnectionRole
  shouldProbe: boolean
} {
  if (!baseUrl || serverMode === 'offline') {
    return { isLoading: false, role: 'unknown', shouldProbe: false }
  }
  if (serverMode === 'checking') {
    return { isLoading: true, role: 'unknown', shouldProbe: false }
  }
  if (serverMode === 'dev') {
    return { isLoading: false, role: 'root', shouldProbe: false }
  }
  const hasTrustedIdentity =
    serverMode === 'trusted' && Boolean(accountId.trim() && userId.trim())
  if (!apiKey && !hasTrustedIdentity) {
    return { isLoading: false, role: 'unknown', shouldProbe: false }
  }
  return { isLoading: true, role: 'unknown', shouldProbe: true }
}

export function shouldRedirectToLoginOnApiError(
  error: unknown,
  isClientError: (value: unknown) => boolean = isOvClientError,
): boolean {
  if (!isClientError(error)) {
    return false
  }

  const clientError = error as { code?: string; statusCode?: number }
  return (
    clientError.statusCode === 401 || clientError.code === 'UNAUTHENTICATED'
  )
}

function applyConnection(
  connection: ConnectionDraft,
  serverMode: ServerMode,
): void {
  ovClient.setOptions({
    baseUrl: connection.baseUrl,
    apiKeyAuth: resolveApiKeyAuth(
      connection.baseUrl,
      connection.serviceSelection,
    ),
  })
  ovClient.setConnection({
    accountId: connection.accountId,
    adminApiKey: connection.adminApiKey,
    apiKey: connection.apiKey,
    identityHeaders: serverMode === 'trusted',
    userId: connection.userId,
  })
}

export function synchronizeConnectionRuntime(
  connection: ConnectionDraft,
  serverMode: ServerMode,
): ConnectionDraft {
  const normalized = normalizeConnectionDraft(connection)
  // Keep the imperative request client ahead of the React tree. Identity
  // changes remount route content, whose child effects may start requests
  // before this provider's passive effects run.
  applyConnection(normalized, serverMode)
  persistConnection(normalized)
  return normalized
}

type ConnectionIdentity = {
  accountId: string
  role: ConnectionRole
  userId: string
}

export function createConnectionHealthHeaders(
  connection: ConnectionDraft,
  credential: 'control' | 'data' = 'control',
): Record<string, string> {
  const headers: Record<string, string> = {}
  const auth = resolveApiKeyAuth(
    connection.baseUrl,
    connection.serviceSelection,
  )
  const apiKey =
    auth === 'bearer'
      ? connection.apiKey
      : credential === 'data'
        ? connection.apiKey || connection.adminApiKey
        : connection.adminApiKey || connection.apiKey
  if (apiKey) {
    headers[auth === 'bearer' ? 'Authorization' : 'X-API-Key'] =
      auth === 'bearer' ? `Bearer ${apiKey}` : apiKey
  }
  const isHosted = auth === 'bearer'
  if (!isHosted && connection.accountId) {
    headers['X-OpenViking-Account'] = connection.accountId
  }
  if (!isHosted && connection.userId) {
    headers['X-OpenViking-User'] = connection.userId
  }
  return headers
}

async function canListAccounts(connection: ConnectionDraft): Promise<boolean> {
  if (
    !connection.adminApiKey ||
    identifyServiceProvider(connection.baseUrl) === 'volcengine' ||
    connection.serviceSelection === 'volcengine'
  ) {
    return false
  }

  try {
    await fetchAdminAccounts({
      accountId: connection.accountId,
      apiKey: connection.adminApiKey,
      baseUrl: connection.baseUrl,
      userId: connection.userId,
    })
    return true
  } catch {
    return false
  }
}

async function detectConnectionIdentity(
  connection: ConnectionDraft,
  credential: 'control' | 'data' = 'control',
): Promise<ConnectionIdentity> {
  const data = await fetchServerHealth(
    connection.baseUrl,
    createConnectionHealthHeaders(connection, credential),
  )

  // /health resolves the presented key and echoes back its identity:
  // { role, account_id, user_id }. We use role to gate the admin UI and
  // account_id to pin the assumed account for an account-admin key.
  const healthRole = isConnectionRole(data.role) ? data.role : 'unknown'
  // In trusted mode /health resolves the asserted tenant user, even when the
  // configured Root key is the credential authorizing Admin API calls. Probe
  // the actual control-plane endpoint so Root capabilities reflect what the
  // browser can really do.
  const role =
    credential === 'control' &&
    connection.adminApiKey &&
    (await canListAccounts(connection))
      ? 'root'
      : healthRole

  return {
    accountId: typeof data.account_id === 'string' ? data.account_id : '',
    role,
    userId: typeof data.user_id === 'string' ? data.user_id : '',
  }
}

export function synchronizeResolvedDataIdentity(
  connection: ConnectionDraft,
  identity: ConnectionIdentity,
): ConnectionDraft | null {
  if (
    (identity.role !== 'admin' && identity.role !== 'user') ||
    !identity.accountId ||
    !identity.userId ||
    (connection.accountId === identity.accountId &&
      connection.userId === identity.userId)
  ) {
    return null
  }

  return {
    ...connection,
    accountId: identity.accountId,
    userId: identity.userId,
  }
}

export function resolveSwitchedIdentity(
  requested: Pick<ConnectionDraft, 'accountId' | 'userId'>,
  identity: ConnectionIdentity,
  allowLegacyIdentityFallback = false,
): Pick<ConnectionDraft, 'accountId' | 'userId'> | null {
  if (
    (identity.role === 'unknown' && !allowLegacyIdentityFallback) ||
    (identity.accountId && identity.accountId !== requested.accountId) ||
    (identity.userId &&
      requested.userId &&
      identity.userId !== requested.userId)
  ) {
    return null
  }

  return {
    accountId: identity.accountId || requested.accountId,
    userId: identity.userId || requested.userId,
  }
}

export function createManagementAccountConnection(
  connection: ConnectionDraft,
  accountId: string,
): ConnectionDraft {
  return {
    ...connection,
    accountId: accountId.trim(),
    apiKey: '',
    userId: '',
  }
}

function readInitialConnection(): ConnectionDraft {
  const storedConnection = readStoredConnection()
  const adminApiKey =
    ENV_ADMIN_API_KEY ||
    storedConnection.adminApiKey ||
    DEFAULT_CONNECTION.adminApiKey
  const apiKey = resolveInitialApiKey({
    defaultApiKey: DEFAULT_CONNECTION.apiKey,
    envApiKey: ENV_API_KEY,
    storedApiKey: storedConnection.apiKey,
  })
  return normalizeConnectionDraft({
    ...DEFAULT_CONNECTION,
    ...storedConnection,
    accountId: resolveIdentityField(
      ENV_ACCOUNT,
      storedConnection.accountId,
      DEFAULT_CONNECTION.accountId,
    ),
    adminApiKey,
    apiKey,
    baseUrl:
      ENV_BASE_URL || storedConnection.baseUrl || DEFAULT_CONNECTION.baseUrl,
    userId: resolveIdentityField(
      ENV_USER,
      storedConnection.userId,
      DEFAULT_CONNECTION.userId,
    ),
  })
}

export function summarizeConnectionIdentity(
  connection: ConnectionDraft,
  serverMode: ServerMode,
): ConnectionIdentitySummary {
  if (serverMode === 'dev') {
    return { labelKey: 'identitySummary.dev' }
  }

  const segments = [connection.accountId, connection.userId].filter(Boolean)
  if (!segments.length) {
    return { labelKey: 'identitySummary.unset' }
  }

  return {
    labelKey: 'identitySummary.named',
    values: {
      identity: segments.join(' / '),
    },
  }
}

export function useAppConnection(): AppConnectionContextValue {
  const context = React.useContext(AppConnectionContext)
  if (!context) {
    throw new Error(
      'useAppConnection must be used within AppConnectionProvider.',
    )
  }

  return context
}

export function AppConnectionProvider({
  children,
}: {
  children: React.ReactNode
}) {
  const queryClient = useQueryClient()
  const authPromptSuppressedUntilRef = React.useRef(0)
  const navigate = useNavigate()
  const pathname = useRouterState({
    select: (state) => state.location.pathname,
  })
  const initialConnectionRef = React.useRef<ConnectionDraft | null>(null)
  const synchronizedRoleProbeRef = React.useRef<{
    key: string
    role: ConnectionRole
  } | null>(null)
  if (initialConnectionRef.current === null) {
    initialConnectionRef.current = readInitialConnection()
    applyConnection(initialConnectionRef.current, 'checking')
  }

  const [connection, setConnection] = React.useState<ConnectionDraft>(
    initialConnectionRef.current,
  )
  const [connectionRole, setConnectionRole] =
    React.useState<ConnectionRole>('unknown')
  const [isConnectionRoleLoading, setConnectionRoleLoading] = React.useState(
    () =>
      Boolean(
        initialConnectionRef.current?.baseUrl &&
        (initialConnectionRef.current.adminApiKey ||
          initialConnectionRef.current.apiKey),
      ),
  )
  const [serverMode, setServerMode] = React.useState<ServerMode>('checking')
  const [serverHealth, setServerHealth] = React.useState<{
    key: string
    data: Record<string, unknown>
  } | null>(null)
  const [connectionAttempt, setConnectionAttempt] = React.useState(0)
  const reconnect = React.useCallback(() => {
    authPromptSuppressedUntilRef.current =
      Date.now() + AUTH_PROMPT_SUPPRESSION_MS
    synchronizedRoleProbeRef.current = null
    resetServerHealthCache()
    setServerHealth(null)
    setServerMode('checking')
    setConnectionRole('unknown')
    setConnectionRoleLoading(true)
    queryClient.clear()
    setConnectionAttempt((attempt) => attempt + 1)
  }, [queryClient])
  const [generatedCredential, setGeneratedCredential] =
    React.useState<GeneratedCredential | null>(null)

  const openConnectionSettings = React.useCallback(() => {
    if (pathname !== '/settings') {
      void navigate({ to: '/settings' })
    }
  }, [navigate, pathname])

  React.useEffect(() => {
    applyConnection(connection, serverMode)
    persistConnection(connection)
  }, [connection, serverMode])

  React.useEffect(() => {
    let cancelled = false

    setServerMode('checking')
    setServerHealth(null)
    void fetchServerHealth(
      connection.baseUrl,
      createConnectionHealthHeaders(connection, 'data'),
    )
      .then((health) => {
        if (!cancelled) {
          const mode = resolveServerAuthMode(health)
          const resolved = synchronizeResolvedDataIdentity(connection, {
            accountId:
              typeof health.account_id === 'string' ? health.account_id : '',
            userId: typeof health.user_id === 'string' ? health.user_id : '',
            role: isConnectionRole(health.role) ? health.role : 'unknown',
          })
          // Resolve the data identity before exposing a ready workspace. A slow
          // or expired management credential must not delay personal navigation.
          const next = resolved
            ? synchronizeConnectionRuntime(resolved, mode)
            : connection
          if (resolved) {
            queryClient.clear()
            setConnection(next)
          }
          setServerHealth({
            key: createConnectionRoleProbeKey(next, 'checking'),
            data: health,
          })
          setServerMode(mode)
        }
      })
      .catch(() => {
        if (!cancelled) {
          setServerHealth(null)
          setServerMode('offline')
        }
      })

    return () => {
      cancelled = true
    }
  }, [
    connection.accountId,
    connection.adminApiKey,
    connection.apiKey,
    connection.baseUrl,
    connection.serviceSelection,
    connection.userId,
    connectionAttempt,
    queryClient,
  ])

  React.useEffect(() => {
    let cancelled = false
    const isCancelled = () => cancelled
    const apiKey = connection.adminApiKey || connection.apiKey
    const roleProbe = resolveConnectionRoleProbeState({
      accountId: connection.accountId,
      apiKey,
      baseUrl: connection.baseUrl,
      serverMode,
      userId: connection.userId,
    })
    const probeKey = createConnectionRoleProbeKey(connection, serverMode)
    const synchronizedProbe = synchronizedRoleProbeRef.current

    if (synchronizedProbe?.key === probeKey) {
      synchronizedRoleProbeRef.current = null
      setConnectionRole(synchronizedProbe.role)
      setConnectionRoleLoading(false)
      return () => {
        cancelled = true
      }
    }

    setConnectionRole(roleProbe.role)
    setConnectionRoleLoading(roleProbe.isLoading)
    if (!roleProbe.shouldProbe) {
      return () => {
        cancelled = true
      }
    }

    void detectConnectionIdentity(connection)
      .catch(() => ({
        accountId: '',
        role: 'unknown' as const,
        userId: '',
      }))
      .then(async (controlIdentity) => {
        if (isCancelled()) {
          return
        }
        const { accountId, role } = controlIdentity
        // An account-admin Root key is scoped to its own account. Pin that
        // account as the assumed identity so admin and data calls target the
        // right tenant instead of failing with a mismatch (the server rejects
        // a foreign account with "ADMIN can only manage account: <x>"). A root
        // key is not account-scoped, so its account selection is left intact.
        if (
          role === 'admin' &&
          (!connection.apiKey ||
            connection.apiKey === connection.adminApiKey) &&
          accountId &&
          connection.accountId !== accountId
        ) {
          const next = synchronizeConnectionRuntime(
            { ...connection, accountId },
            serverMode,
          )
          synchronizedRoleProbeRef.current = {
            key: createConnectionRoleProbeKey(next, serverMode),
            role,
          }
          queryClient.clear()
          setConnection(next)
          return
        }
        setConnectionRole(role)
        setConnectionRoleLoading(false)
      })
      .catch(() => {
        if (!cancelled) {
          setConnectionRole('unknown')
          setConnectionRoleLoading(false)
        }
      })

    return () => {
      cancelled = true
    }
  }, [
    connection.accountId,
    connection.adminApiKey,
    connection.apiKey,
    connection.baseUrl,
    connection.serviceSelection,
    connection.userId,
    queryClient,
    serverMode,
  ])

  React.useEffect(() => {
    const interceptorId = ovClient.instance.interceptors.response.use(
      (response) => response,
      (error) => {
        if (
          shouldRedirectToLoginOnApiError(error) &&
          Date.now() >= authPromptSuppressedUntilRef.current
        ) {
          openConnectionSettings()
        }
        return Promise.reject(error)
      },
    )

    return () => {
      ovClient.instance.interceptors.response.eject(interceptorId)
    }
  }, [openConnectionSettings])

  const value = React.useMemo<AppConnectionContextValue>(() => {
    const commitConnection = (next: ConnectionDraft) => {
      authPromptSuppressedUntilRef.current =
        Date.now() + AUTH_PROMPT_SUPPRESSION_MS
      const normalized = synchronizeConnectionRuntime(next, serverMode)
      queryClient.clear()
      setConnection(normalized)
    }

    return {
      clearGeneratedCredential: () => setGeneratedCredential(null),
      connection,
      connectionRole,
      generatedCredential,
      identityScopeKey: createIdentityScopeKey(connection, serverMode),
      isConnectionRoleLoading,
      openConnectionSettings,
      reconnect,
      serverHealth:
        serverHealth?.key ===
        createConnectionRoleProbeKey(connection, 'checking')
          ? serverHealth.data
          : null,
      saveConnection: commitConnection,
      setGeneratedCredential,
      serverMode,
      switchIdentity: async ({
        accountId,
        allowLegacyIdentityFallback,
        apiKey,
        userId,
      }) => {
        if (serverMode === 'dev' || serverMode === 'checking') {
          throw createConnectionError('identitySwitchUnsupported')
        }

        const requested = normalizeConnectionDraft({
          ...connection,
          accountId,
          apiKey: serverMode === 'trusted' ? '' : apiKey,
          userId,
        })
        const identity = await detectConnectionIdentity(requested, 'data')
        const resolvedIdentity = resolveSwitchedIdentity(
          requested,
          identity,
          allowLegacyIdentityFallback,
        )
        if (!resolvedIdentity) {
          throw createConnectionError('credentialMismatch')
        }

        if (pathname === '/directory' || pathname === '/memories') {
          await navigate({ replace: true, to: pathname })
        }
        if (pathname === '/playground') {
          await navigate({
            replace: true,
            search: { upload: false },
            to: '/playground',
          })
        }
        commitConnection({
          ...requested,
          ...resolvedIdentity,
        })
      },
      switchManagementAccount: async (accountId) => {
        if (connectionRole !== 'root') {
          throw createConnectionError('rootRequired')
        }
        commitConnection(
          createManagementAccountConnection(connection, accountId),
        )
      },
    }
  }, [
    connection,
    connectionRole,
    generatedCredential,
    isConnectionRoleLoading,
    navigate,
    openConnectionSettings,
    pathname,
    queryClient,
    reconnect,
    serverHealth,
    serverMode,
  ])

  return (
    <AppConnectionContext.Provider value={value}>
      {children}
    </AppConnectionContext.Provider>
  )
}
