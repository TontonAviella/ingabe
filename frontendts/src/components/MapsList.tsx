import { Loader2, Plus, Trash2 } from 'lucide-react';
import { Link } from 'react-router-dom';
import { toast } from 'sonner';
import { type IndustryKey, useIndustry, useIndustryKnown } from '@/components/IndustryGate';
import { IndustryTabs } from '@/components/IndustryTabs';
import type { MapProject } from '@/lib/types';
import { useProjects } from '../contexts/ProjectsContext';
import { Button } from './ui/button';
import { Pagination, PaginationContent, PaginationItem, PaginationLink, PaginationNext, PaginationPrevious } from './ui/pagination';

// The home: the maps of the industry the user works in now. One title, one caramel button (New map); the industry
// and its count are on the tabs, so the title does not repeat them. Why a list failed goes to telemetry
// (ProjectsContext), never on screen: a person can only try again.

const PLACEHOLDERS = 4;

/** "Edited 3 days ago", from the map's last edit (or its creation). */
function editedAgo(project: MapProject): string {
  const diffSec = Math.floor((Date.now() - new Date(project.most_recent_version?.last_edited || project.created_on).getTime()) / 1000);
  const [value, unit]: [number, Intl.RelativeTimeFormatUnit] =
    diffSec < 60
      ? [diffSec, 'second']
      : diffSec < 3600
        ? [Math.floor(diffSec / 60), 'minute']
        : diffSec < 86400
          ? [Math.floor(diffSec / 3600), 'hour']
          : [Math.floor(diffSec / 86400), 'day'];
  return `Edited ${new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' }).format(-value, unit)}`;
}

export default function MapsList() {
  // A new project takes its industry at creation: wait until it is known (audit R1-31).
  const industryKnown = useIndustryKnown();
  const {
    projects,
    totalPages,
    totalItems,
    industryLabel,
    otherIndustries,
    currentPage,
    showDeleted,
    isLoading,
    isCreating,
    error,
    setCurrentPage,
    setShowDeleted,
    createProject,
    deleteProject,
    refetchProjects,
  } = useProjects();
  const industry = useIndustry().data;
  // Maps per industry for the tabs: the listed industry's total, and the server's count for each of the others.
  const counts: Partial<Record<IndustryKey, number>> = Object.fromEntries(otherIndustries.map((o) => [o.industry, o.count]));
  if (industry?.industry && totalItems !== null) counts[industry.industry] = totalItems;
  // The tabs name the industry; without them (one industry only, or none chosen) the title does.
  const tabsShown = !!industry && industry.can_choose && !industry.locked && industry.industry !== null;
  const title = showDeleted ? 'Recently deleted' : tabsShown ? 'Maps' : (industryLabel ?? 'Maps');

  const handleCreateMap = async () => {
    try {
      await createProject();
    } catch {
      toast.error("Couldn't make a new map. Try again.");
    }
  };

  const handleDeleteMap = async (projectId: string, event: React.MouseEvent) => {
    event.preventDefault(); // the card is a link: deleting must not open the map
    event.stopPropagation();
    try {
      await deleteProject(projectId);
    } catch {
      toast.error("Couldn't delete the map. Try again.");
    }
  };

  const pagination =
    totalPages > 1 ? (
      <Pagination>
        <PaginationContent>
          <PaginationItem>
            <PaginationPrevious
              onClick={() => currentPage > 1 && setCurrentPage(currentPage - 1)}
              className={currentPage <= 1 ? 'pointer-events-none opacity-50' : 'cursor-pointer'}
            />
          </PaginationItem>
          {Array.from({ length: totalPages }, (_, i) => i + 1)
            .filter((page) => totalPages <= 5 || Math.abs(page - currentPage) <= 2)
            .map((page) => (
              <PaginationItem key={page}>
                <PaginationLink onClick={() => setCurrentPage(page)} isActive={currentPage === page} className="cursor-pointer">
                  {page}
                </PaginationLink>
              </PaginationItem>
            ))}
          <PaginationItem>
            <PaginationNext
              onClick={() => currentPage < totalPages && setCurrentPage(currentPage + 1)}
              className={currentPage >= totalPages ? 'pointer-events-none opacity-50' : 'cursor-pointer'}
            />
          </PaginationItem>
        </PaginationContent>
      </Pagination>
    ) : null;

  const newMap = (
    <Button onClick={handleCreateMap} disabled={isCreating || !industryKnown}>
      {isCreating ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : <Plus className="h-4 w-4" aria-hidden="true" />}
      New map
    </Button>
  );

  return (
    <div className="w-full min-w-0 flex flex-col gap-6 p-4 sm:p-6 sm:min-w-xl">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold sm:text-2xl">{title}</h1>
        <div className="flex items-center gap-2">
          <Button variant="ghost" aria-pressed={showDeleted} onClick={() => setShowDeleted(!showDeleted)} className="text-fg-muted">
            {showDeleted ? 'Back to maps' : 'Recently deleted'}
          </Button>
          {newMap}
        </div>
      </div>

      <IndustryTabs counts={counts} />

      {error ? (
        <div role="alert" className="flex flex-col items-center gap-4 rounded-xl border border-line bg-panel p-8 text-center">
          <p className="font-medium">Couldn't load your maps</p>
          <Button variant="outline" onClick={refetchProjects}>
            Try again
          </Button>
        </div>
      ) : isLoading ? (
        <div role="status" className="grid gap-4 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-6">
          <span className="sr-only">Loading maps</span>
          {Array.from({ length: PLACEHOLDERS }, (_, i) => (
            <div key={i} aria-hidden="true" className="h-[280px] animate-pulse rounded-xl border border-line bg-panel" />
          ))}
        </div>
      ) : projects.length === 0 ? (
        <p className="rounded-xl border border-dashed border-line bg-canvas/60 p-10 text-center text-fg-muted">
          {showDeleted ? 'Nothing deleted recently' : 'No maps yet'}
        </p>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-6">
          {projects.map((project) => {
            const deleted = project.soft_deleted_at != null;
            return (
              <Link
                to={`/project/${project.id}`}
                key={project.id}
                className="group relative block h-[280px] overflow-hidden rounded-xl border border-line bg-panel bg-cover bg-center focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-caramel"
                style={
                  deleted
                    ? {
                        backgroundImage:
                          'repeating-linear-gradient(45deg, var(--color-raised) 0px, var(--color-raised) 10px, var(--color-line) 10px, var(--color-line) 20px)',
                      }
                    : { backgroundImage: `url(/api/projects/${project.id}/social.webp)` }
                }
              >
                {/* Darkens the map picture so the title reads on any map. */}
                <div className="absolute inset-0 bg-gradient-to-b from-black/60 via-black/10 to-black/60 transition-colors group-hover:via-black/25" />
                {!deleted && project.is_owner !== false && (
                  <button
                    type="button"
                    onClick={(e) => handleDeleteMap(project.id, e)}
                    aria-label={`Delete ${project.title}`}
                    title="Delete map"
                    className="absolute top-2 right-2 z-10 cursor-pointer rounded-md bg-canvas/80 p-1.5 text-fg-soft opacity-0 transition-opacity hover:text-bad focus-visible:opacity-100 group-hover:opacity-100"
                  >
                    <Trash2 className="h-4 w-4" aria-hidden="true" />
                  </button>
                )}
                <div className="relative flex h-full flex-col justify-between p-4 text-fg">
                  <h3 className="line-clamp-2 pr-8 text-lg font-semibold">{project.title}</h3>
                  <p className="text-xs text-fg-soft">{editedAgo(project)}</p>
                </div>
              </Link>
            );
          })}
        </div>
      )}

      {pagination}
    </div>
  );
}
