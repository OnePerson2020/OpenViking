import { createFileRoute } from '@tanstack/react-router'
import { DirectoryPage } from './-directory-page'

export const Route = createFileRoute('/directory')({ component: DirectoryPage })
