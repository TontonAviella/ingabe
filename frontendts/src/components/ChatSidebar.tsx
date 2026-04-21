'use client';

import {
  Brain,
  Brush,
  ChevronDown,
  ChevronUp,
  CloudDownload,
  Edit3,
  MapPin,
  MapPlus,
  MessageCirclePlus,
  Minus,
  MousePointerClick,
  PanelLeftClose,
  PanelLeftOpen,
  Satellite,
  Send,
  SquareTerminal,
  TextSearch,
  Upload,
  User,
  Wrench,
  ZoomIn,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import SyntaxHighlighter from 'react-syntax-highlighter';
import { dark } from 'react-syntax-highlighter/dist/esm/styles/hljs';
import remarkGfm from 'remark-gfm';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { QgisIcon } from '@/lib/qgis';
import type {
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
    case 'qgis':
      return <QgisIcon className="w-4 h-4" />;
    case 'square-terminal':
      return <SquareTerminal className="w-4 h-4" />;
    case 'satellite':
      return <Satellite className="w-4 h-4" />;
    case 'map-pin':
      return <MapPin className="w-4 h-4" />;
  }
}

function isExpandable(toolCall: SanitizedToolCall) {
  return toolCall.code || toolCall.table;
}

const KUE_MESSAGE_STYLE = `
  text-sm
  [&_table]:w-full [&_table]:border-collapse [&_table]:text-left
  [&_thead]:border-b-1 [&_thead]:border-gray-600
  [&_thead_th]:font-semibold
  [&_tbody_tr]:border-b [&_tbody_tr]:border-gray-200 last:[&_tbody_tr]:border-b-0
  [&_td]:align-top
  [&_img]:border [&_img]:border-[#aaa] [&_img]:rounded-md [&_img]:my-2 [&_img]:block [&_img]:mx-auto [&_img]:max-w-[360px] [&_img]:h-auto
  [&_pre]:max-w-80 [&_pre]:overflow-x-scroll [&_pre]:my-4 [&_pre]:bg-gray-900 [&_pre]:border-gray-500 [&_pre]:border [&_pre]:rounded
`;

function MessageItem({
  message,
  expandedToolCalls,
  setExpandedToolCalls,
  toolResponses,
}: {
  message: SanitizedMessage;
  expandedToolCalls: string[];
  setExpandedToolCalls: (toolCalls: string[]) => void;
  toolResponses: SanitizedToolResponse[];
}) {
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
        className={`px-3 py-2 rounded-lg ${
          message.role === 'user'
            ? 'bg-blue-600/20 border border-blue-500/30 ml-6'
            : 'bg-gray-700/50 mr-6'
        }`}
      >
        <div className="flex items-center gap-1.5 mb-1">
          {message.role === 'user' ? (
            <User className="w-3 h-3 text-blue-400 shrink-0" />
          ) : (
            <Brain className="w-3 h-3 text-green-400 shrink-0" />
          )}
          <span className="text-[10px] font-medium text-gray-400 uppercase tracking-wider">
            {message.role === 'user' ? 'You' : 'Sage'}
          </span>
        </div>
        <div className={`${KUE_MESSAGE_STYLE} text-white text-halfway-sm-xs`}>
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
        </div>
      </div>
      {message.tool_calls && message.tool_calls.length > 0 && (
        <div className="px-3 space-y-1">
          {message.tool_calls.map((toolCall) => {
            const status = toolStatusLookup[toolCall.id] ?? 'pending';
            return (
              <div key={toolCall.id} className="space-y-1">
                <div
                  className={`flex items-center gap-2 text-xs ${toolColorLookup[status]} ${
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
                  <div className="shrink-0">{iconForToolCall(toolCall)}</div>
                  <div className="truncate">{toolCall.tagline}</div>
                  {status !== 'pending' && (
                    <span
                      className={`shrink-0 text-[10px] px-1.5 py-0.5 rounded-full font-medium ${
                        status === 'success'
                          ? 'bg-green-900/50 text-green-400'
                          : 'bg-red-900/50 text-red-400'
                      }`}
                    >
                      {status === 'success' ? 'Complete' : 'Failed'}
                    </span>
                  )}
                  {isExpandable(toolCall) && (
                    <div className="shrink-0">
                      {expandedToolCalls.includes(toolCall.id) ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                    </div>
                  )}
                </div>
                {toolCall.code && expandedToolCalls.includes(toolCall.id) && (
                  <pre className="text-left bg-gray-800 rounded text-xs overflow-x-scroll">
                    <SyntaxHighlighter
                      language={toolCall.code.language}
                      style={dark}
                      className="rounded border-gray-500 border bg-slate-900!"
                    >
                      {toolCall.code.code}
                    </SyntaxHighlighter>
                  </pre>
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

interface ChatSidebarProps {
  mapTree: MapTreeResponse | null;
  conversationId: number | null;
  currentMapId: string | null;
  conversations: Conversation[];
  setConversationId: (conversationId: number | null) => void;
  activeActions: EphemeralAction[];
  conversationsEnabled?: boolean;
  onSendMessage: (text: string) => void;
  hasSelectedFeature: boolean;
  onClearSelectedFeature?: () => void;
  isCollapsed: boolean;
  onToggleCollapse: () => void;
}

export default function ChatSidebar({
  mapTree,
  conversationId,
  currentMapId,
  conversations,
  setConversationId,
  activeActions,
  conversationsEnabled = true,
  onSendMessage,
  hasSelectedFeature,
  onClearSelectedFeature,
  isCollapsed,
  onToggleCollapse,
}: ChatSidebarProps) {
  const [inputValue, setInputValue] = useState('');
  const [expandedToolCalls, setExpandedToolCalls] = useState<string[]>([]);
  const [expandedEditGroups, setExpandedEditGroups] = useState<string[]>([]);
  const [isConversationsExpanded, setIsConversationsExpanded] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && inputValue.trim()) {
      e.preventDefault();
      onSendMessage(inputValue);
      setInputValue('');
    }
  };

  const handleSendClick = () => {
    if (inputValue.trim()) {
      onSendMessage(inputValue);
      setInputValue('');
    }
  };

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

  const getEditIcon = useCallback((node: MapNode) => {
    if (!node.diff_from_previous) return null;
    const { added_layers, removed_layers } = node.diff_from_previous;
    if (added_layers.length > 0 && removed_layers.length > 0) return <Edit3 className="w-3 h-3" />;
    if (added_layers.length > 0) return <Upload className="w-3 h-3" />;
    if (removed_layers.length > 0) return <Minus className="w-3 h-3" />;
    return <Edit3 className="w-3 h-3" />;
  }, []);

  const getEditDescription = useCallback((node: MapNode) => {
    if (!node.diff_from_previous) return 'Created map';
    const { added_layers, removed_layers } = node.diff_from_previous;
    const parts = [];
    if (added_layers.length > 0) parts.push(added_layers.map((l) => l.name).join(', '));
    if (removed_layers.length > 0) parts.push(removed_layers.map((l) => l.name).join(', '));
    return parts.length > 0 ? parts.join(', ') : 'Layer changes';
  }, []);

  const MIN_GROUP_SIZE = 3;
  type DisplayItem = { type: 'node'; node: MapNode } | { type: 'group'; id: string; nodes: MapNode[] };

  const displayItems: DisplayItem[] = useMemo(() => {
    if (!mapTree) return [];
    const items: DisplayItem[] = [];
    const nodes = mapTree.tree;
    let i = 0;
    while (i < nodes.length) {
      const node = nodes[i];
      const messages = getMessagesForMap(node.map_id);
      if (messages.length === 0) {
        let j = i;
        while (j < nodes.length && getMessagesForMap(nodes[j].map_id).length === 0) j++;
        if (j - i >= MIN_GROUP_SIZE) {
          const groupNodes = nodes.slice(i, j);
          items.push({
            type: 'group',
            id: `group-${groupNodes[0].map_id}-${groupNodes[groupNodes.length - 1].map_id}`,
            nodes: groupNodes,
          });
          i = j;
          continue;
        }
        items.push({ type: 'node', node });
        i++;
      } else {
        items.push({ type: 'node', node });
        i++;
      }
    }
    return items;
  }, [mapTree, getMessagesForMap]);

  useEffect(() => {
    if (!currentMapId) return;
    const containing = displayItems.find(
      (it) => it.type === 'group' && it.nodes.some((n) => n.map_id === currentMapId),
    ) as Extract<DisplayItem, { type: 'group' }> | undefined;
    if (containing) {
      setExpandedEditGroups((prev) => (prev.includes(containing.id) ? prev : [...prev, containing.id]));
    }
  }, [currentMapId, displayItems]);

  // Auto-scroll to bottom when new messages arrive
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [displayItems, activeActions]);

  const toggleGroup = (groupId: string) => {
    setExpandedEditGroups((prev) => (prev.includes(groupId) ? prev.filter((id) => id !== groupId) : [...prev, groupId]));
  };

  const renderMapNode = (node: MapNode) => {
    const messages = getMessagesForMap(node.map_id);
    const toolResponses = messages.filter((msg) => msg.role === 'tool' && msg.tool_response).map((msg) => msg.tool_response!);
    const hasMessages = messages.length > 0;
    const hasDiff = node.diff_from_previous && (node.diff_from_previous.added_layers.length > 0 || node.diff_from_previous.removed_layers.length > 0);

    return (
      <div key={node.map_id} className="space-y-1.5">
        {/* Edit indicator (layer changes) */}
        {hasDiff && (
          <div className="flex items-center gap-2 px-3 text-[11px] text-gray-500">
            <div className="shrink-0">{getEditIcon(node)}</div>
            <span className="truncate">{getEditDescription(node)}</span>
            <span className="shrink-0">{formatShortRelativeTime(node.created_on)}</span>
          </div>
        )}

        {/* Messages */}
        {hasMessages &&
          messages.map((msg, msgIndex) => (
            <MessageItem
              key={`msg-${node.map_id}-${msgIndex}`}
              message={msg}
              expandedToolCalls={expandedToolCalls}
              setExpandedToolCalls={setExpandedToolCalls}
              toolResponses={toolResponses}
            />
          ))}
      </div>
    );
  };

  if (!conversationsEnabled) return null;

  return (
    <div
      className="flex flex-col h-full bg-gray-800 border-r border-gray-700 transition-all duration-300 ease-in-out overflow-hidden"
      style={{
        width: isCollapsed ? '0px' : '380px',
        minWidth: isCollapsed ? '0px' : '350px',
        maxWidth: isCollapsed ? '0px' : '500px',
      }}
    >
      {!isCollapsed && (
        <>
          {/* Header */}
          <div className="flex items-center justify-between px-3 py-2 border-b border-gray-700 shrink-0">
            <div className="flex items-center gap-2">
              <Brain className="w-4 h-4 text-green-400" />
              <span className="text-sm font-semibold text-gray-100">Sage</span>
            </div>
            <div className="flex items-center gap-1">
              <Tooltip>
                <TooltipTrigger asChild>
                  <button
                    className={`p-1 rounded ${
                      conversationId === null
                        ? 'text-gray-600 cursor-not-allowed'
                        : 'text-gray-400 hover:text-gray-200 hover:bg-gray-700 cursor-pointer'
                    }`}
                    onClick={conversationId === null ? undefined : () => setConversationId(null)}
                  >
                    <MessageCirclePlus className="w-4 h-4" />
                  </button>
                </TooltipTrigger>
                <TooltipContent>
                  <p>{conversationId === null ? 'Already in new chat' : 'New chat'}</p>
                </TooltipContent>
              </Tooltip>
              <Tooltip>
                <TooltipTrigger asChild>
                  <button
                    className="p-1 text-gray-400 hover:text-gray-200 hover:bg-gray-700 rounded cursor-pointer"
                    onClick={onToggleCollapse}
                  >
                    <PanelLeftClose className="w-4 h-4" />
                  </button>
                </TooltipTrigger>
                <TooltipContent>
                  <p>Collapse chat</p>
                </TooltipContent>
              </Tooltip>
            </div>
          </div>

          {/* Previous conversations (collapsible) */}
          {conversations.length > 0 && (
            <div className="border-b border-gray-700 shrink-0">
              <button
                className="flex items-center justify-between w-full px-3 py-1.5 text-xs text-gray-400 hover:text-gray-200 hover:bg-gray-750 cursor-pointer"
                onClick={() => setIsConversationsExpanded(!isConversationsExpanded)}
              >
                <span>
                  {conversations.length} previous chat{conversations.length !== 1 ? 's' : ''}
                </span>
                {isConversationsExpanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
              </button>
              {isConversationsExpanded && (
                <div className="max-h-40 overflow-y-auto pb-1">
                  {conversations.map((conversation) => (
                    <div
                      key={conversation.id}
                      className={`text-xs py-1.5 px-3 cursor-pointer ${
                        conversation.id === conversationId
                          ? 'bg-blue-900/50 text-blue-200'
                          : 'text-gray-400 hover:bg-gray-700 hover:text-gray-200'
                      }`}
                      onClick={() => setConversationId(conversation.id)}
                    >
                      <div className="flex items-center justify-between min-w-0">
                        <span className="truncate font-medium">{conversation.title}</span>
                        <span className="shrink-0 text-gray-500 ml-2">{formatShortRelativeTime(conversation.updated_at)}</span>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* Messages area (scrollable) */}
          <div className="flex-1 overflow-y-auto px-1 py-2 space-y-2">
            {displayItems.length === 0 && (
              <div className="flex flex-col items-center justify-center h-full text-gray-500 px-6 text-center">
                <Brain className="w-8 h-8 mb-3 text-gray-600" />
                <p className="text-sm font-medium text-gray-400">Ask Sage anything</p>
                <p className="text-xs mt-1">
                  Search data, change map styles, run geoprocessing, or ask questions about your layers.
                </p>
              </div>
            )}

            {displayItems.map((item) => {
              if (item.type === 'node') return renderMapNode(item.node);
              const { id, nodes } = item;
              const isOpen = expandedEditGroups.includes(id);
              return (
                <div key={id}>
                  <button
                    className="flex items-center gap-2 px-3 py-1 text-[11px] text-gray-500 hover:text-gray-300 cursor-pointer"
                    onClick={() => toggleGroup(id)}
                  >
                    {isOpen ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                    <Edit3 className="w-3 h-3" />
                    <span>
                      {nodes.length} {isOpen ? 'visible' : 'hidden'} edits
                    </span>
                  </button>
                  {isOpen && nodes.map((n) => renderMapNode(n))}
                </div>
              );
            })}

            {/* Active actions ("Sage is thinking...") */}
            {activeActions.length > 0 && (
              <div className="px-3 py-2 rounded-lg bg-green-900/20 border border-green-500/20 mr-6">
                <div className="flex items-center gap-1.5 mb-1">
                  <Brain className="w-3 h-3 text-green-400 animate-spin" />
                  <span className="text-[10px] font-medium text-green-400 uppercase tracking-wider">Sage</span>
                </div>
                <div className="space-y-1">
                  {activeActions.map((action, idx) => (
                    <div key={`${action.action_id}-${idx}`} className="flex items-center gap-2 text-xs text-gray-300">
                      <span className="animate-pulse">{action.action}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div ref={messagesEndRef} />
          </div>

          {/* Input area (fixed at bottom) */}
          <div className="shrink-0 border-t border-gray-700 p-2">
            <div className="flex items-end gap-2 bg-gray-900 rounded-lg border border-gray-600 focus-within:border-gray-400 transition-colors">
              <textarea
                className="flex-1 bg-transparent text-sm text-white placeholder-gray-500 resize-none px-3 py-2 focus:outline-none min-h-[36px] max-h-[120px]"
                placeholder="Ask Sage..."
                rows={1}
                value={inputValue}
                onChange={(e) => {
                  setInputValue(e.target.value);
                  e.target.style.height = 'auto';
                  e.target.style.height = Math.min(e.target.scrollHeight, 120) + 'px';
                }}
                onKeyDown={handleKeyDown}
              />
              <div className="flex items-center gap-1 pr-2 pb-2">
                {hasSelectedFeature && (
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <button
                        onClick={onClearSelectedFeature}
                        className="text-blue-400 hover:text-blue-300 cursor-pointer"
                      >
                        <MousePointerClick className="w-4 h-4" />
                      </button>
                    </TooltipTrigger>
                    <TooltipContent>
                      <p>Sage can see your selected feature. Click to deselect.</p>
                    </TooltipContent>
                  </Tooltip>
                )}
                <button
                  onClick={handleSendClick}
                  disabled={!inputValue.trim()}
                  className={`p-1 rounded ${
                    inputValue.trim()
                      ? 'text-green-400 hover:text-green-300 hover:bg-gray-700 cursor-pointer'
                      : 'text-gray-600 cursor-not-allowed'
                  }`}
                >
                  <Send className="w-4 h-4" />
                </button>
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

export function ChatSidebarToggle({
  isCollapsed,
  onToggle,
}: {
  isCollapsed: boolean;
  onToggle: () => void;
}) {
  if (!isCollapsed) return null;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          className="absolute top-3 left-3 z-40 p-2 bg-gray-800/90 hover:bg-gray-700 text-gray-300 hover:text-white rounded-lg shadow-lg backdrop-blur-sm cursor-pointer transition-colors"
          onClick={onToggle}
        >
          <PanelLeftOpen className="w-5 h-5" />
        </button>
      </TooltipTrigger>
      <TooltipContent>
        <p>Open chat</p>
      </TooltipContent>
    </Tooltip>
  );
}
