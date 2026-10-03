import { featureForPath } from './studio-service'
import type { StudioDomain } from './studio-service'

// Validate remembered pages against the current connection's visible features.
export function resolveDomainDestination(
  domain: StudioDomain,
  rememberedPath: string | undefined,
  availablePaths: readonly string[],
): string {
  const paths = availablePaths.filter(
    (path) => featureForPath(path)?.domain === domain,
  )
  const rememberedFeature = rememberedPath && featureForPath(rememberedPath)
  if (
    rememberedFeature &&
    rememberedFeature.domain === domain &&
    paths.includes(rememberedFeature.to)
  )
    return rememberedPath
  return paths[0] ?? '/directory'
}
