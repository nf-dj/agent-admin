import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

/**
 * Renders markdown source as styled HTML.
 *
 * In its own lazy-loaded module so the ~80KB markdown bundle is only
 * pulled in when a user actually opens a skill detail page \u2014 it shouldn't
 * cost the dashboard.
 *
 * GFM plugin enabled for tables / strikethrough / task lists, which
 * real-world SKILL.md files use heavily.
 *
 * Links open in a new tab (skill manifests sometimes reference third-party
 * docs); ``rel="noreferrer"`` for privacy.
 */
export function MarkdownView({ source }: { source: string }) {
  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: (props) => <a {...props} target="_blank" rel="noreferrer" />,
        }}
      >
        {source}
      </ReactMarkdown>
    </div>
  );
}

export default MarkdownView;
