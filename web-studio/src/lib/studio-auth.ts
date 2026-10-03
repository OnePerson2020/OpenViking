import { resolveConfiguredServiceProvider } from './studio-service'
import type { ServiceSelection } from './studio-service'

export type ApiKeyAuth = 'header' | 'bearer'

export function resolveApiKeyAuth(
  baseUrl: string,
  selection: ServiceSelection = 'auto',
): ApiKeyAuth {
  return resolveConfiguredServiceProvider(baseUrl, selection) === 'volcengine'
    ? 'bearer'
    : 'header'
}
