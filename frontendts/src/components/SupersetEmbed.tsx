import { useState, useEffect, useRef } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Activity, BarChart3, ExternalLink } from 'lucide-react';
import { useSupersetStatus, useSupersetGuestToken } from '@/hooks/useRwandaApi';

interface SupersetEmbedProps {
  dashboardId?: string;
  title?: string;
  height?: string;
}

export function SupersetEmbed({ dashboardId, title = 'Analytics Dashboard', height = '600px' }: SupersetEmbedProps) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const { data: status, isLoading: statusLoading } = useSupersetStatus();
  const { mutate: getGuestToken, data: tokenData, isPending: tokenLoading } = useSupersetGuestToken();
  const [embedUrl, setEmbedUrl] = useState<string | null>(null);

  // Get guest token when dashboard ID is provided and Superset is available
  useEffect(() => {
    if (dashboardId && status?.available) {
      getGuestToken(dashboardId);
    }
  }, [dashboardId, status?.available, getGuestToken]);

  // Build embed URL when token is available
  useEffect(() => {
    if (tokenData?.token) {
      setEmbedUrl(`http://localhost:8088/superset/dashboard/${dashboardId}/?standalone=1&guest_token=${tokenData.token}`);
    }
  }, [tokenData, dashboardId]);

  // Superset not available state
  if (!statusLoading && (!status || !status.available)) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <BarChart3 className="size-5" />
            {title}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex flex-col items-center justify-center py-12 text-center">
            <BarChart3 className="size-12 text-muted-foreground/50 mb-4" />
            <h3 className="text-lg font-semibold mb-2">Apache Superset Not Available</h3>
            <p className="text-sm text-muted-foreground max-w-md">
              The analytics service is not currently running. Start Superset with{' '}
              <code className="bg-muted px-1.5 py-0.5 rounded text-xs">docker compose up superset</code>
            </p>
            <Badge variant="secondary" className="mt-4">
              Service Offline
            </Badge>
          </div>
        </CardContent>
      </Card>
    );
  }

  // No dashboard selected
  if (!dashboardId) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <BarChart3 className="size-5" />
            {title}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex flex-col items-center justify-center py-12 text-center">
            <BarChart3 className="size-12 text-muted-foreground/50 mb-4" />
            <h3 className="text-lg font-semibold mb-2">Superset Analytics</h3>
            <p className="text-sm text-muted-foreground max-w-md">
              Create dashboards in{' '}
              <a
                href="http://localhost:8088"
                target="_blank"
                rel="noopener noreferrer"
                className="text-primary hover:underline inline-flex items-center gap-1"
              >
                Apache Superset <ExternalLink className="size-3" />
              </a>
              {' '}to embed analytics here.
            </p>
            <div className="flex items-center gap-2 mt-4">
              <span className="size-2 rounded-full bg-green-500 animate-pulse" />
              <span className="text-xs text-muted-foreground">Superset is running</span>
            </div>
          </div>
        </CardContent>
      </Card>
    );
  }

  // Loading state
  if (statusLoading || tokenLoading) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <BarChart3 className="size-5" />
            {title}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex items-center justify-center py-12">
            <Activity className="size-6 animate-spin text-muted-foreground" />
            <span className="ml-2 text-sm text-muted-foreground">Loading analytics...</span>
          </div>
        </CardContent>
      </Card>
    );
  }

  // Embedded dashboard
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2">
            <BarChart3 className="size-5" />
            {title}
          </CardTitle>
          <a
            href={`http://localhost:8088/superset/dashboard/${dashboardId}/`}
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1"
          >
            Open in Superset <ExternalLink className="size-3" />
          </a>
        </div>
      </CardHeader>
      <CardContent className="p-0">
        {embedUrl ? (
          <iframe
            ref={iframeRef}
            src={embedUrl}
            title={title}
            style={{ width: '100%', height, border: 'none' }}
            sandbox="allow-scripts allow-same-origin allow-popups allow-forms"
          />
        ) : (
          <div className="flex items-center justify-center" style={{ height }}>
            <p className="text-sm text-muted-foreground">Unable to load dashboard</p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
