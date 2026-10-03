import { resolveApiKeyAuth } from '#/lib/studio-auth'
import { useQueries } from '@tanstack/react-query'
import { useAppConnection } from './use-app-connection'
import { useStudioService } from './use-studio-service'
import {
  STUDIO_FEATURES,
  classifyCapabilityError,
  classifyCapabilityResponse,
} from '#/lib/studio-service'
import { normalizeOvClientError, createOvClient } from '#/lib/ov-client'

// Only inexpensive, object-free GETs. Do not probe writes, imports, or search.
export function useStudioCapabilities() {
  const { connection, identityScopeKey } = useAppConnection()
  const service = useStudioService()
  const features = STUDIO_FEATURES.filter((f) => 'probe' in f)
  const results = useQueries({
    queries: features.map((feature) => ({
      queryKey: ['studio-feature', identityScopeKey, feature.id],
      enabled: service.ready && service.provider === 'volcengine',
      queryFn: async ({ signal }: { signal: AbortSignal }) => {
        try {
          // Capture credentials with the query's scope. A mutable global client
          // can otherwise attach a new key to an old connection's request.
          const client = createOvClient({
            apiKeyStorageKey: '',
            apiKeyAuth: resolveApiKeyAuth(
              connection.baseUrl,
              connection.serviceSelection,
            ),
            baseUrl: connection.baseUrl,
            connection: {
              apiKey:
                resolveApiKeyAuth(
                  connection.baseUrl,
                  connection.serviceSelection,
                ) === 'bearer'
                  ? connection.apiKey
                  : connection.apiKey || connection.adminApiKey,
            },
          })
          const response = await client.instance.get(feature.probe, {
            baseURL: connection.baseUrl.replace(/\/+$/, ''),
            signal,
            timeout: 8_000,
          })
          return classifyCapabilityResponse(feature.id, response.data)
        } catch (error) {
          return classifyCapabilityError(normalizeOvClientError(error))
        }
      },
      retry: false,
      staleTime: 60_000,
    })),
  })
  const capabilities: Partial<
    Record<(typeof STUDIO_FEATURES)[number]['id'], (typeof results)[number]>
  > = Object.fromEntries(
    features.map((feature, index) => [feature.id, results[index]]),
  )
  return capabilities
}
