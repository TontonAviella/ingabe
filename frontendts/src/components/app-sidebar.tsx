import { AccountMenu, OrgSwitcher } from '@mundi/ee';
import { Check, House, Loader2, Mountain, PanelRightClose, PanelRightOpen } from 'lucide-react';
import { Suspense } from 'react';
import { Link } from 'react-router-dom';
import { IngabeIcon, IngabeWordmark } from '@/components/Brand';
import { OPEN_INDUSTRY_PICKER, useIndustry, useSwitchIndustry } from '@/components/IndustryGate';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from '@/components/ui/sidebar';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { useProjects } from '@/contexts/ProjectsContext';
import type { MapProject } from '@/lib/types';
import { formatRelativeTime } from '@/lib/utils';

/** "Your industry": a menu of the industries, the current one ticked; choosing another switches at once. */
function IndustryMenu() {
  const { data } = useIndustry();
  const { switchTo, pending } = useSwitchIndustry();
  if (!data || !data.can_choose || data.locked || data.industry === null) return null;
  const current = data.options.find((o) => o.key === data.industry);
  return (
    <SidebarMenuItem>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <SidebarMenuButton tooltip={`Your industry: ${current?.label ?? ''}`}>
            {pending ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <Mountain className="w-4 h-4 mr-2" />}
            <span className="flex min-w-0 flex-col text-left leading-tight">
              <span className="text-xs text-gray-500">Your industry</span>
              <span className="truncate text-sm">{current?.label}</span>
            </span>
          </SidebarMenuButton>
        </DropdownMenuTrigger>
        <DropdownMenuContent side="right" align="start" className="min-w-52">
          <DropdownMenuLabel>Switch industry</DropdownMenuLabel>
          {data.options.map((option) => (
            <DropdownMenuItem
              key={option.key}
              disabled={pending !== null}
              onSelect={() => option.key !== data.industry && switchTo(option.key, option.label)}
              className="cursor-pointer"
            >
              <Check className={`mr-2 h-4 w-4 ${option.key === data.industry ? 'opacity-100' : 'opacity-0'}`} />
              {option.label}
            </DropdownMenuItem>
          ))}
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={() => window.dispatchEvent(new Event(OPEN_INDUSTRY_PICKER))} className="cursor-pointer text-gray-500">
            What each industry covers…
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </SidebarMenuItem>
  );
}

export function AppSidebar() {
  const { state, toggleSidebar } = useSidebar();
  const { allProjects, allProjectsLoading } = useProjects();

  const recentProjects: MapProject[] = [...allProjects] // sort a copy: the array belongs to the query cache
    .sort(
      (a, b) => new Date(b.most_recent_version?.last_edited || '').getTime() - new Date(a.most_recent_version?.last_edited || '').getTime(),
    )
    .slice(0, 3);

  return (
    <Sidebar collapsible="icon" data-theme="light" className="border-none">
      <SidebarHeader className="flex flex-col items-center p-4">
        {state === 'collapsed' ? (
          <>
            <a href="/" className="w-8 h-8">
              <IngabeIcon className="w-full h-full" />
            </a>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button variant="ghost" size="icon" onClick={toggleSidebar} className="w-8 h-8 mt-2 cursor-pointer">
                  <PanelRightOpen className="w-4 h-4 scale-x-[-1]" />
                </Button>
              </TooltipTrigger>
              <TooltipContent side="right">
                <p>Expand Sidebar</p>
              </TooltipContent>
            </Tooltip>
          </>
        ) : (
          <div className="flex items-center justify-between w-full">
            <a href="/" className="h-8 flex items-center gap-2.5">
              <IngabeIcon className="h-7 w-7" />
              <IngabeWordmark className="text-[19px]" />
            </a>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button variant="ghost" size="icon" onClick={toggleSidebar} className="w-8 h-8 cursor-pointer">
                  <PanelRightClose className="w-4 h-4 scale-x-[-1]" />
                </Button>
              </TooltipTrigger>
              <TooltipContent side="right">
                <p>Collapse Sidebar</p>
              </TooltipContent>
            </Tooltip>
          </div>
        )}
      </SidebarHeader>
      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>Projects</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton asChild tooltip="Home">
                  <Link to={`/`}>
                    <House className="w-4 h-4 mr-2" />
                    <span className="text-sm">Home</span>
                  </Link>
                </SidebarMenuButton>
              </SidebarMenuItem>
              {!allProjectsLoading && state === 'expanded' && (
                <>
                  {recentProjects.map((project) => (
                    <SidebarMenuItem key={project.id}>
                      <SidebarMenuButton asChild>
                        <Link to={`/project/${project.id}`} className="flex items-center justify-between w-full">
                          <span className="text-sm">{project.title || `Untitled Map`}</span>
                          <span className="text-xs text-muted-foreground ml-2">
                            {formatRelativeTime(project.most_recent_version?.last_edited)}
                          </span>
                        </Link>
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  ))}
                </>
              )}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>

        <SidebarGroup>
          <SidebarGroupContent>
            <SidebarMenu>
              <IndustryMenu />
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>

        <Suspense fallback={null}>
          <OrgSwitcher />
          <AccountMenu />
        </Suspense>
      </SidebarContent>
      <SidebarFooter className="p-1 border-t border-border border-gray-700">
        <div className="text-center">
          <span className="text-muted-foreground text-xs">
            {state === 'collapsed' ? (
              <IngabeIcon className="w-6 h-6 mx-auto my-2" />
            ) : (
              <a href={import.meta.env.VITE_NOZA_SITE_URL || 'https://nozalabs.rw'} className="hover:text-foreground">
                © {new Date().getFullYear()} Noza Labs
              </a>
            )}
          </span>
        </div>
      </SidebarFooter>
    </Sidebar>
  );
}
