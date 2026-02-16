import mermaid from 'mermaid';
import React, { useEffect, useRef } from 'react';

mermaid.initialize({
  startOnLoad: false,
  theme: 'dark',
  securityLevel: 'loose',
  fontFamily: 'Fira Code',
});

interface MermaidProps {
  chart: string;
}

const MermaidComponent: React.FC<MermaidProps> = ({ chart }) => {
  const elementRef = useRef<HTMLDivElement>(null);
  const renderIdRef = useRef<string>('');

  useEffect(() => {
    const renderDiagram = async () => {
      if (!elementRef.current) return;

      try {
        const renderId = `mermaid-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
        renderIdRef.current = renderId;

        // Clear the element completely
        elementRef.current.innerHTML = '';
        elementRef.current.removeAttribute('data-processed');

        // Use mermaid.render to get the SVG
        const { svg } = await mermaid.render(renderId, chart);

        // Only update if this is still the current render (prevents race conditions)
        if (renderIdRef.current === renderId && elementRef.current) {
          elementRef.current.innerHTML = svg;
        }
      } catch (error) {
        console.error('Error rendering Mermaid diagram:', error);
        if (elementRef.current) {
          elementRef.current.innerHTML = `<div style="color: red; padding: 10px; border: 1px solid red;">
            Error rendering diagram: ${error instanceof Error ? error.message : 'Unknown error'}
          </div>`;
        }
      }
    };

    renderDiagram();

    // Cleanup function
    return () => {
      if (elementRef.current) {
        elementRef.current.innerHTML = '';
        elementRef.current.removeAttribute('data-processed');
      }
    };
  }, [chart]);

  return <div ref={elementRef} className="mermaid" />;
};

export default MermaidComponent;
