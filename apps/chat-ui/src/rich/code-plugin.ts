import type { code as streamdownCode } from '@streamdown/code'
import { useEffect, useState } from 'react'

type CodePlugin = typeof streamdownCode
let codePluginCache: CodePlugin | null = null

export function useCodePlugin(): CodePlugin | null {
  const [plugin, setPlugin] = useState(codePluginCache)

  useEffect(() => {
    if (plugin) {
      return
    }

    let cancelled = false

    void import('@streamdown/code')
      .then(({ code }) => {
        codePluginCache = code

        if (!cancelled) {
          setPlugin(code)
        }
      })
      .catch(() => {
        /* Plain code remains usable when highlighting is unavailable. */
      })

    return () => {
      cancelled = true
    }
  }, [plugin])

  return plugin
}
