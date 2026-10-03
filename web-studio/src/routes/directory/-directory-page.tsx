import { useState } from 'react'
import { Link } from '@tanstack/react-router'
import {
  FileIcon,
  FolderIcon,
  ArrowLeftIcon,
  RefreshCwIcon,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Button } from '#/components/ui/button'
import { cn } from '#/lib/utils'
import { useAppConnection } from '#/hooks/use-app-connection'
import { useStudioService } from '#/hooks/use-studio-service'
import { useVikingFsList } from '#/routes/resources/-hooks/viking-fm'
import { LazyFilePreview } from '#/routes/resources/-components/lazy-file-preview'
import { normalizeDirUri } from '#/routes/resources/-lib/normalize'
import type { VikingFsEntry } from '#/routes/resources/-types/viking-fm'

export function DirectoryPage({ memories = false }: { memories?: boolean }) {
  const { t } = useTranslation('studio')
  const { connection } = useAppConnection()
  const { provider } = useStudioService()
  const personalRoot =
    provider === 'volcengine'
      ? 'viking://~/'
      : `viking://user/${connection.userId}/`
  const [shared, setShared] = useState(false)
  const rootUri = shared
    ? 'viking://resources/'
    : personalRoot + (memories ? 'memories/' : '')
  const [path, setPath] = useState(rootUri)
  const [history, setHistory] = useState<string[]>([])
  const [selected, setSelected] = useState<VikingFsEntry | null>(null)
  const query = useVikingFsList(path, {
    output: 'agent',
    nodeLimit: 500,
    limit: 500,
    sortBy: 'name',
    sortOrder: 'asc',
  })
  function open(entry: VikingFsEntry) {
    if (entry.isDir) {
      setHistory((previous) => [...previous, path])
      setPath(normalizeDirUri(entry.uri))
      setSelected(null)
    } else setSelected(entry)
  }
  function changeScope(isShared: boolean) {
    setShared(isShared)
    setHistory([])
    setPath(
      isShared
        ? 'viking://resources/'
        : personalRoot + (memories ? 'memories/' : ''),
    )
    setSelected(null)
  }
  const entries = query.data?.entries ?? []
  return (
    <section className="grid min-w-0 gap-4">
      <header>
        <h1 className="text-2xl font-semibold">
          {t(memories ? 'memories' : 'directory')}
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          {t('directoryDescription')}
        </p>
      </header>
      <div
        className="flex flex-wrap items-center gap-2"
        role="group"
        aria-label={t('spaces')}
      >
        <Button
          variant={!shared ? 'default' : 'outline'}
          onClick={() => changeScope(false)}
        >
          {t('personal')}
        </Button>
        <Button
          variant="outline"
          nativeButton={false}
          render={<Link to={memories ? '/directory' : '/memories'} />}
        >
          {t(memories ? 'directory' : 'memories')}
        </Button>
        {!memories ? (
          <Button
            variant={shared ? 'default' : 'outline'}
            onClick={() => changeScope(true)}
          >
            {t('shared')}
          </Button>
        ) : null}
        <Button
          variant="ghost"
          size="icon"
          aria-label={t('retry')}
          onClick={() => void query.refetch()}
        >
          <RefreshCwIcon />
        </Button>
      </div>
      <div className="flex flex-wrap items-center gap-2 rounded-lg border px-3 py-2">
        <Button
          variant="ghost"
          size="icon"
          disabled={history.length === 0}
          aria-label={t('back')}
          onClick={() => {
            setPath(history.at(-1) ?? rootUri)
            setHistory((previous) => previous.slice(0, -1))
            setSelected(null)
          }}
        >
          <ArrowLeftIcon />
        </Button>
        <span className="min-w-0 break-all font-mono text-xs">{path}</span>
      </div>
      <div className="grid min-w-0 rounded-xl border lg:grid-cols-[minmax(220px,1fr)_minmax(0,2fr)]">
        <div
          className={cn(
            'min-w-0 p-3 lg:border-r',
            selected && 'hidden lg:block',
          )}
          aria-live="polite"
        >
          {query.isLoading ? (
            <p className="p-3 text-sm">{t('loading')}</p>
          ) : query.isError ? (
            <div role="alert" className="grid gap-2 p-3 text-sm">
              <p>{t('directoryError')}</p>
              <p className="break-words text-muted-foreground">
                {query.error.message}
              </p>
              <Button variant="outline" onClick={() => void query.refetch()}>
                {t('retry')}
              </Button>
            </div>
          ) : entries.length === 0 ? (
            <p className="p-3 text-sm text-muted-foreground">{t('empty')}</p>
          ) : (
            entries.map((entry) => (
              <button
                key={entry.uri}
                type="button"
                aria-pressed={selected?.uri === entry.uri}
                onClick={() => open(entry)}
                className="flex w-full items-center gap-3 rounded-md px-3 py-3 text-left text-sm hover:bg-muted aria-pressed:bg-muted"
              >
                {entry.isDir ? (
                  <FolderIcon className="size-4 shrink-0" />
                ) : (
                  <FileIcon className="size-4 shrink-0" />
                )}
                <span className="min-w-0 break-all">{entry.name}</span>
              </button>
            ))
          )}
        </div>
        <div
          className={cn(
            'min-h-80 min-w-0 flex-col',
            selected ? 'flex' : 'hidden lg:flex',
          )}
        >
          {selected ? (
            <>
              <div className="border-b p-2 lg:hidden">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setSelected(null)}
                >
                  <ArrowLeftIcon />
                  {t('backToFiles')}
                </Button>
              </div>
              <LazyFilePreview
                readOnly
                file={selected}
                onClose={() => setSelected(null)}
              />
            </>
          ) : (
            <div className="flex flex-1 items-center justify-center p-8 text-sm text-muted-foreground">
              {t('selectFile')}
            </div>
          )}
        </div>
      </div>
      <p className="text-xs text-muted-foreground">
        {t('readOnly')} {t('listingLimit')}
      </p>
    </section>
  )
}
