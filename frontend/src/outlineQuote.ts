import { tr } from './i18n.ts'
export interface OutlineQuote {
  node_id: string
  title: string
  excerpt: string
}

export function quoteQuestion(question: string, quote: OutlineQuote | null | undefined): string {
  return quote ? tr("{0}\n\n引用的大纲原文（{1}）：\n{2}", question, quote.title, quote.excerpt) : question
}
