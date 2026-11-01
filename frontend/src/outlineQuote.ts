export interface OutlineQuote {
  node_id: string
  title: string
  excerpt: string
}

export function quoteQuestion(question: string, quote: OutlineQuote | null | undefined): string {
  return quote ? `${question}\n\n引用的大纲原文（${quote.title}）：\n${quote.excerpt}` : question
}
