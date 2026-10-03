import type { ServiceProvider } from './studio-service'
import type { ConnectionRole } from '#/hooks/use-app-connection'
import type { ServerMode } from '#/hooks/use-server-mode'

export type StudioManagementCapabilities = {
  canManageAccounts: boolean
  canManageUsers: boolean
}

export function resolveStudioManagementCapabilities({
  serviceProvider = 'opensource',
  hasControlCredential,
  isRoleLoading,
  role,
  serverMode,
}: {
  serviceProvider?: ServiceProvider
  hasControlCredential: boolean
  isRoleLoading: boolean
  role: ConnectionRole
  serverMode: ServerMode
}): StudioManagementCapabilities {
  if (
    serviceProvider !== 'opensource' ||
    isRoleLoading ||
    serverMode === 'dev' ||
    !hasControlCredential
  ) {
    return {
      canManageAccounts: false,
      canManageUsers: false,
    }
  }

  return {
    canManageAccounts: role === 'root',
    canManageUsers: role === 'root' || role === 'admin',
  }
}
