import { TextMessagePartProvider } from '@assistant-ui/react'
import {
  type StreamdownTextComponents,
  StreamdownTextPrimitive,
  type SyntaxHighlighterProps,
  tailBoundedRemend
} from '@assistant-ui/react-streamdown'
import { Component, type ComponentProps, createContext, type ReactNode, useContext, useMemo } from 'react'
import { Block, type BlockProps } from 'streamdown'

import { detectArtifact } from './artifact-detect'
import { BoundedOutput, exceedsMarkdownBudget } from './bounded-output'
import { ArtifactView, MediaView } from './cards'
import { safeExternalUrl } from './contracts'
import { createMemoizedMathPlugin } from './katex-memo'
import { parseMarkdownIntoBlocksCached } from './markdown-blocks'
import { preprocessMarkdown } from './markdown-preprocess'

const BlockIndex = createContext(0)

function IndexedBlock(props: BlockProps) {
  return (
    <BlockIndex.Provider value={props.index}>
      <Block {...props} />
    </BlockIndex.Provider>
  )
}

function Code({
  code,
  language,
  sourceId,
  streaming
}: SyntaxHighlighterProps & { sourceId: string; streaming: boolean }) {
  const index = useContext(BlockIndex)
  const detection = detectArtifact(language, code)

  return detection ? (
    <ArtifactView artifact={{ id: `${sourceId}:block:${index}`, ...detection, text: code }} />
  ) : (
    <BoundedOutput language={streaming ? undefined : language} text={code} />
  )
}

const plugins = { math: createMemoizedMathPlugin({ singleDollarTextMath: true }) }
const security = { allowedProtocols: ['https', 'http'], allowDataImages: false }

function preprocess(text: string) {
  try {
    return tailBoundedRemend(preprocessMarkdown(text))
  } catch {
    return text
  }
}

class RenderBoundary extends Component<{ children: ReactNode; text: string }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() {
    return { failed: true }
  }
  render() {
    return this.state.failed ? <BoundedOutput text={this.props.text} /> : this.props.children
  }
}

function SafeLink({ href = '', children }: ComponentProps<'a'>) {
  const url = safeExternalUrl(href)

  return url ? (
    <a href={url} rel="noopener noreferrer" target="_blank">
      {children}
    </a>
  ) : (
    <span>{children}</span>
  )
}

export interface RichMarkdownProps {
  text: string
  sourceId: string
  streaming?: boolean
}

/** Same streaming engine as Desktop; all open/preview actions go through the host. */
export function RichMarkdown({ text, sourceId, streaming = false }: RichMarkdownProps) {
  const components = useMemo(
    () =>
      ({
        a: SafeLink,
        img: ({ src, alt }: ComponentProps<'img'>) =>
          typeof src === 'string' && safeExternalUrl(src) ? (
            <MediaView reference={{ kind: 'image', value: src, title: alt || 'Image' }} />
          ) : (
            <span>{alt || 'Image unavailable'}</span>
          ),
        SyntaxHighlighter: (props: SyntaxHighlighterProps) => (
          <Code {...props} sourceId={sourceId} streaming={streaming} />
        )
      }) as StreamdownTextComponents,
    [sourceId, streaming]
  )

  if (exceedsMarkdownBudget(text)) {
    return <BoundedOutput text={text} />
  }

  return (
    <RenderBoundary text={text}>
      <TextMessagePartProvider isRunning={streaming} text={text}>
        <StreamdownTextPrimitive
          BlockComponent={IndexedBlock}
          components={components}
          containerClassName="hermes-rich-markdown"
          parseIncompleteMarkdown={false}
          parseMarkdownIntoBlocksFn={parseMarkdownIntoBlocksCached}
          plugins={plugins}
          preprocess={preprocess}
          security={security}
          skipHtml
        />
      </TextMessagePartProvider>
    </RenderBoundary>
  )
}
