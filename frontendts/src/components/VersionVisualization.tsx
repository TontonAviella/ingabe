'use client';

import {
  ArrowLeft,
  Brain,
  Brush,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  CloudDownload,
  Edit3,
  MapPlus,
  Minus,
  Plus,
  Search,
  SquareTerminal,
  TextSearch,
  Upload,
  Wrench,
  ZoomIn,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import SyntaxHighlighter from 'react-syntax-highlighter';
import { dark } from 'react-syntax-highlighter/dist/esm/styles/hljs';
import remarkGfm from 'remark-gfm';
import { PANEL_FONT } from '@/components/CardAnswer';
import { ReportAudienceSelect } from '@/components/ReportAudienceSelect';
import { Answer } from '@/components/SageReply';
import {
  Conversation,
  EphemeralAction,
  MapNode,
  MapTreeResponse,
  SanitizedMessage,
  SanitizedToolCall,
  SanitizedToolResponse,
} from '@/lib/types';
import { formatShortRelativeTime } from '../lib/utils';

function iconForToolCall(toolCall: SanitizedToolCall) {
  switch (toolCall.icon) {
    case 'text-search':
      return <TextSearch className="w-4 h-4" />;
    case 'brush':
      return <Brush className="w-4 h-4" />;
    case 'wrench':
      return <Wrench className="w-4 h-4" />;
    case 'map-plus':
      return <MapPlus className="w-4 h-4" />;
    case 'cloud-download':
      return <CloudDownload className="w-4 h-4" />;
    case 'zoom-in':
      return <ZoomIn className="w-4 h-4" />;
    case 'square-terminal':
      return <SquareTerminal className="w-4 h-4" />;
  }
}

function isExpandable(toolCall: SanitizedToolCall) {
  return toolCall.code || toolCall.table;
}

function MessageItem({
  message,
  msgIndex,
  expandedToolCalls,
  setExpandedToolCalls,
  toolResponses,
}: {
  message: SanitizedMessage;
  msgIndex: number;
  expandedToolCalls: string[];
  setExpandedToolCalls: (toolCalls: string[]) => void;
  toolResponses: SanitizedToolResponse[];
}) {
  // Create lookup table from toolCall.id to toolStatus
  const toolStatusLookup =
    message.tool_calls?.reduce(
      (acc, toolCall) => {
        const toolResponse = toolResponses.find((response) => response.id === toolCall.id);
        acc[toolCall.id] = toolResponse?.status || 'pending';
        return acc;
      },
      {} as Record<string, string>,
    ) || {};

  const toolColorLookup: Record<string, string> = {
    success: 'text-muted-foreground',
    error: 'text-red-400',
    pending: 'text-gray-300',
  };

  const toolHoverColorLookup: Record<string, string> = {
    success: 'hover:text-gray-100',
    error: 'hover:text-red-300',
    pending: 'hover:text-gray-100',
  };

  return (
    <>
      <div
        key={msgIndex}
        className={`text-halfway-sm-xs ${message.role === 'user' ? 'rounded-[14px] bg-[#221813] py-1.5 px-3 max-w-[85%] ml-auto' : 'px-0.5'}`}
      >
        <div className="flex-1 min-w-0 text-[14px] text-[#F3EDE6] [&_pre]:max-w-80 [&_pre]:overflow-x-scroll [&_pre]:my-4 [&_pre]:bg-gray-900 [&_pre]:border-gray-500 [&_pre]:border [&_pre]:rounded">
          {message.role === 'assistant' && message.content ? (
            <Answer text={message.content} />
          ) : (
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
          )}
        </div>
      </div>
      {message.tool_calls && message.tool_calls.length > 0 && (
        <div>
          {message.tool_calls.map((toolCall) => {
            const status = toolStatusLookup[toolCall.id] ?? 'pending';
            return (
              <div key={toolCall.id} className="space-y-1">
                <div
                  className={`flex justify-start gap-2 ${toolColorLookup[status]} ${
                    status === 'pending' ? 'animate-pulse' : ''
                  } ${isExpandable(toolCall) ? `cursor-pointer ${toolHoverColorLookup[status]}` : ''}`}
                  onClick={() => {
                    if (isExpandable(toolCall)) {
                      const isExpanded = expandedToolCalls.includes(toolCall.id);
                      if (isExpanded) {
                        setExpandedToolCalls(expandedToolCalls.filter((id) => id !== toolCall.id));
                      } else {
                        setExpandedToolCalls([...expandedToolCalls, toolCall.id]);
                      }
                    }
                  }}
                  title={status === 'error' ? 'Tool call failed' : status === 'pending' ? 'Tool call pending' : 'Tool call succeeded'}
                >
                  <div>{iconForToolCall(toolCall)}</div>
                  <div>{toolCall.tagline}</div>
                  {isExpandable(toolCall) && (
                    <div>
                      {expandedToolCalls.includes(toolCall.id) ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                    </div>
                  )}
                </div>
                {toolCall.code && expandedToolCalls.includes(toolCall.id) && (
                  <div>
                    <pre className="text-left bg-gray-800 rounded text-xs overflow-x-scroll">
                      <SyntaxHighlighter
                        language={toolCall.code.language}
                        style={dark}
                        className="rounded border-gray-500 border bg-slate-900!"
                      >
                        {toolCall.code.code}
                      </SyntaxHighlighter>
                    </pre>
                  </div>
                )}
                {toolCall.table && expandedToolCalls.includes(toolCall.id) && (
                  <div className="text-left bg-slate-900 border-gray-500 border rounded text-xs overflow-x-scroll">
                    <table className="w-full border-collapse">
                      <tbody>
                        {Object.entries(toolCall.table).map(([key, value], index, array) => (
                          <tr key={key} className={index < array.length - 1 ? 'border-b border-gray-600' : ''}>
                            <td className="px-2 py-1 font-medium text-gray-300">{key}</td>
                            <td className="px-2 py-1 text-white">{value}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </>
  );
}

interface VersionVisualizationProps {
  mapTree: MapTreeResponse | null;
  conversationId: number | null;
  currentMapId: string | null;
  conversations: Conversation[];
  setConversationId: (conversationId: number | null) => void;
  activeActions: EphemeralAction[];
  conversationsEnabled?: boolean;
  farmControls?: boolean; // the report-view selector; agriculture projects only
}

// The chats panel: like a chat app's sidebar. With no chat open, "New chat", a search and the recent chats, newest
// first, one line each, with the map's changes folded away below. With a chat open, its messages as a plain
// conversation; the map edits made along the way are one quiet line each (Roger, 2026-10-26: the old timeline of
// avatars and the "Type message and hit enter to chat" button looked unprofessional).

const PENDING_TITLE = 'title pending';
const chatName = (c: Conversation | undefined) => (c?.title && c.title !== PENDING_TITLE ? c.title : 'New chat');
const PANEL = 'bg-[#17110E] text-[#F3EDE6]';
const ROW =
  'flex w-full items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-left text-[14px] cursor-pointer hover:bg-white/[0.06] focus-visible:outline-2 focus-visible:outline-[#D9A066]';

export default function VersionVisualization({
  mapTree,
  conversationId,
  currentMapId,
  conversations,
  setConversationId,
  activeActions,
  conversationsEnabled = true,
  farmControls = true,
}: VersionVisualizationProps) {
  const [expandedToolCalls, setExpandedToolCalls] = useState<string[]>([]);
  const [expandedEditGroups, setExpandedEditGroups] = useState<string[]>([]);
  const [search, setSearch] = useState('');
  const [changesOpen, setChangesOpen] = useState(false);

  // Helper function to get messages for a specific map_id
  const getMessagesForMap = useCallback(
    (mapId: string) => {
      const node = mapTree?.tree.find((n) => n.map_id === mapId);
      if (!node) return [];
      return node.messages
        .filter((msg) => msg.role !== 'system')
        .sort((a, b) => {
          if (a.created_at && b.created_at) {
            return new Date(a.created_at).getTime() - new Date(b.created_at).getTime();
          }
          return 0;
        });
    },
    [mapTree],
  );

  // One plain line per map change: "Added X", "Removed Y", "Changed Z".
  const describeEdit = useCallback((node: MapNode) => {
    if (!node.diff_from_previous) return { icon: <MapPlus className="size-3.5" />, text: 'Created the map' };
    const { added_layers, removed_layers } = node.diff_from_previous;
    const names = (layers: { name: string }[]) => layers.map((l) => l.name).join(', ');
    if (added_layers.length > 0 && removed_layers.length > 0) {
      return { icon: <Edit3 className="size-3.5" />, text: `Changed ${names(added_layers)}` };
    }
    if (added_layers.length > 0) return { icon: <Upload className="size-3.5" />, text: `Added ${names(added_layers)}` };
    if (removed_layers.length > 0) return { icon: <Minus className="size-3.5" />, text: `Removed ${names(removed_layers)}` };
    return { icon: <Edit3 className="size-3.5" />, text: 'Changed the layers' };
  }, []);

  // Runs of 3 or more map changes with no messages fold into "N map changes".
  const MIN_GROUP_SIZE = 3;
  type DisplayItem = { type: 'node'; node: MapNode } | { type: 'group'; id: string; nodes: MapNode[] };
  const displayItems: DisplayItem[] = useMemo(() => {
    if (!mapTree) return [];
    const items: DisplayItem[] = [];
    const nodes = mapTree.tree;
    let i = 0;
    while (i < nodes.length) {
      if (getMessagesForMap(nodes[i].map_id).length === 0) {
        let j = i;
        while (j < nodes.length && getMessagesForMap(nodes[j].map_id).length === 0) j++;
        if (j - i >= MIN_GROUP_SIZE) {
          const groupNodes = nodes.slice(i, j);
          const groupId = `conv-${conversationId ?? 'new'}-group-${groupNodes[0].map_id}-${groupNodes[groupNodes.length - 1].map_id}`;
          items.push({ type: 'group', id: groupId, nodes: groupNodes });
          i = j;
          continue;
        }
      }
      items.push({ type: 'node', node: nodes[i] });
      i++;
    }
    return items;
  }, [mapTree, getMessagesForMap, conversationId]);

  // Keep the group holding the map on screen open.
  useEffect(() => {
    if (!currentMapId) return;
    const containing = displayItems.find((it) => it.type === 'group' && it.nodes.some((n) => n.map_id === currentMapId)) as
      | Extract<DisplayItem, { type: 'group' }>
      | undefined;
    if (containing) {
      setExpandedEditGroups((prev) => (prev.includes(containing.id) ? prev : [...prev, containing.id]));
    }
  }, [currentMapId, displayItems]);

  const toggleGroup = (groupId: string) => {
    setExpandedEditGroups((prev) => (prev.includes(groupId) ? prev.filter((id) => id !== groupId) : [...prev, groupId]));
  };

  const editLine = (node: MapNode) => {
    const { icon, text } = describeEdit(node);
    const bySage = node.fork_reason === 'ai_edit';
    const here = node.map_id === currentMapId;
    return (
      <div
        key={`edit-${node.map_id}`}
        className={`grid grid-cols-[16px_minmax(0,1fr)_auto] items-baseline gap-2 rounded-[8px] px-2.5 py-1.5 text-[13px] ${here ? 'text-[#F3EDE6]' : 'text-[#B8A99B]'}`}
        title={new Date(node.created_on).toLocaleString()}
      >
        <span className="text-[#8E7F73]">{icon}</span>
        <span className="truncate">
          {text}
          {bySage && <span className="text-[#D9A066]"> · Sage</span>}
        </span>
        <span className="text-[12px] text-[#8E7F73]">{formatShortRelativeTime(node.created_on)}</span>
      </div>
    );
  };

  const nodeWithMessages = (node: MapNode) => {
    const messages = getMessagesForMap(node.map_id);
    const toolResponses = messages.filter((msg) => msg.role === 'tool' && msg.tool_response).map((msg) => msg.tool_response!);
    return (
      <div key={node.map_id} className="flex flex-col gap-1.5">
        {node.diff_from_previous && editLine(node)}
        {messages.map((msg, msgIndex) => (
          <MessageItem
            key={`message-${node.map_id}-${msgIndex}`}
            message={msg}
            msgIndex={msgIndex}
            expandedToolCalls={expandedToolCalls}
            setExpandedToolCalls={setExpandedToolCalls}
            toolResponses={toolResponses}
          />
        ))}
      </div>
    );
  };

  const changesFolded = (id: string, nodes: MapNode[]) => {
    const open = expandedEditGroups.includes(id);
    return (
      <div key={id}>
        <button type="button" className={`${ROW} text-[13px] text-[#B8A99B]`} onClick={() => toggleGroup(id)} aria-expanded={open}>
          <ChevronRight className={`size-3.5 transition-transform ${open ? 'rotate-90' : ''}`} />
          {nodes.length} map changes
        </button>
        {open && <div className="pl-3">{nodes.map((n) => editLine(n))}</div>}
      </div>
    );
  };

  if (!conversationsEnabled) return null;

  const current = conversations.find((c) => c.id === conversationId);
  const query = search.trim().toLowerCase();
  const recent = conversations.filter((c) => chatName(c).toLowerCase().includes(query));
  const working = activeActions.length > 0;

  return (
    <div className={`z-30 max-h-screen h-full w-full flex flex-col ${PANEL}`} style={{ fontFamily: PANEL_FONT }}>
      {conversationId === null ? (
        <>
          <div className="flex flex-col gap-1 px-2.5 pt-3 pb-1">
            <button type="button" className={ROW} onClick={() => setConversationId(null)} title="Ask Sage below to start a chat">
              <Plus className="size-4 text-[#D9A066]" />
              New chat
            </button>
            <label className="flex items-center gap-2.5 rounded-[9px] bg-[#221813] px-2.5 py-1.5">
              <Search className="size-4 text-[#B8A99B]" />
              <input
                id="chat-search"
                type="search"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search chats"
                aria-label="Search chats"
                className="min-w-0 flex-1 bg-transparent text-[14px] text-[#F3EDE6] placeholder:text-[#8E7F73] outline-none"
              />
            </label>
            {/* Farmer, insurer, agronomist, scientist: report views for agriculture projects only (audit R1-28). */}
            {farmControls && <ReportAudienceSelect label="Answers written for" compact />}
          </div>
          <div className="px-5 pt-3 pb-1.5 text-[12px] font-semibold text-[#8E7F73]">Recents</div>
          <div className="min-h-0 flex-1 overflow-y-auto px-2.5" role="list" aria-label="Recent chats">
            {recent.length === 0 && (
              <p className="m-0 px-2.5 py-2 text-[13px] text-[#8E7F73]">
                {conversations.length === 0 ? 'No chats yet. Ask Sage below to start one.' : 'No chat matches.'}
              </p>
            )}
            {recent.map((c) => (
              <button
                key={c.id}
                type="button"
                role="listitem"
                className={`${ROW} group justify-between`}
                onClick={() => setConversationId(c.id)}
                title={`${c.message_count} messages`}
              >
                <span className="min-w-0 truncate">{chatName(c)}</span>
                <span className="shrink-0 text-[12px] text-[#8E7F73] opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100">
                  {formatShortRelativeTime(c.updated_at)}
                </span>
              </button>
            ))}
          </div>
          {mapTree && mapTree.tree.length > 0 && (
            <div className="border-t border-white/[0.07] px-2.5 pt-1.5 pb-3">
              <button
                type="button"
                className={`${ROW} text-[13px] text-[#B8A99B]`}
                onClick={() => setChangesOpen((v) => !v)}
                aria-expanded={changesOpen}
              >
                <ChevronRight className={`size-3.5 transition-transform ${changesOpen ? 'rotate-90' : ''}`} />
                Map changes
                <span className="ml-auto text-[12px] text-[#8E7F73]">{mapTree.tree.length}</span>
              </button>
              {changesOpen && <div className="max-h-56 overflow-y-auto pl-3">{[...mapTree.tree].reverse().map((n) => editLine(n))}</div>}
            </div>
          )}
        </>
      ) : (
        <>
          <div className="flex items-center gap-1 border-b border-white/[0.07] px-2 py-2">
            <button
              type="button"
              className="flex size-8 shrink-0 items-center justify-center rounded-[8px] text-[#B8A99B] hover:bg-white/[0.06] hover:text-[#F3EDE6] cursor-pointer"
              onClick={() => setConversationId(null)}
              aria-label="All chats"
              title="All chats"
            >
              <ArrowLeft className="size-4" />
            </button>
            <span className="min-w-0 flex-1 truncate text-[14px] font-semibold">{chatName(current)}</span>
            <button
              type="button"
              className="flex size-8 shrink-0 items-center justify-center rounded-[8px] text-[#D9A066] hover:bg-white/[0.06] cursor-pointer"
              onClick={() => setConversationId(null)}
              aria-label="New chat"
              title="New chat"
            >
              <Plus className="size-4" />
            </button>
          </div>
          <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-3 py-3">
            {displayItems.map((item) =>
              item.type === 'group'
                ? changesFolded(item.id, item.nodes)
                : getMessagesForMap(item.node.map_id).length > 0
                  ? nodeWithMessages(item.node)
                  : editLine(item.node),
            )}
            {working && (
              <div className="flex items-center gap-2 px-2.5 py-1.5 text-[13px] text-[#B8A99B]">
                <Brain className="size-4 animate-pulse text-[#D9A066]" /> Sage is working…
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
