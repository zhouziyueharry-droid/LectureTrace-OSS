import { useState } from 'react'
import { errorText, respondToAgent, type AgentInteraction as Interaction } from '../api.ts'

export default function AgentInteraction({ videoId, request, onResolved }: {
  videoId: string; request: Interaction; onResolved: () => void
}) {
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const questions = request.params.questions
  const choices = request.params.availableDecisions ?? ['accept', 'decline']
  const respond = async (decision: string) => {
    setBusy(true)
    setError(null)
    try { await respondToAgent(videoId, request.id, decision, answers); onResolved() }
    catch (err) { setError(errorText(err)); setBusy(false) }
  }
  return <form className="ag-interaction" aria-label={questions ? 'Agent 需要补充信息' : 'Agent 请求操作许可'}
    onSubmit={event => { event.preventDefault(); void respond(questions ? 'answer' : 'accept') }}>
    <strong>{questions ? '需要你补充一点信息' : '这项操作需要你的许可'}</strong>
    {questions ? questions.map(question => <fieldset key={question.id} disabled={busy}>
      <legend>{question.question}</legend>
      {question.options?.map(option => <label className="ag-choice" key={option.label}>
        <input type="radio" name={request.id + question.id} checked={answers[question.id] === option.label}
          onChange={() => setAnswers(old => ({ ...old, [question.id]: option.label }))} />
        <span>{option.label}{option.description && <small>{option.description}</small>}</span>
      </label>)}
      <input aria-label={`${question.header}：填写回答`} type={question.isSecret ? 'password' : 'text'}
        value={answers[question.id] ?? ''} placeholder="也可以直接填写回答"
        onChange={event => setAnswers(old => ({ ...old, [question.id]: event.target.value }))} />
    </fieldset>) : <>
      {request.params.reason && <p>{request.params.reason}</p>}
      {request.params.cwd && <p className="small">工作目录：{request.params.cwd}</p>}
      {request.params.grantRoot && <p>申请写入目录：{request.params.grantRoot}</p>}
      {request.params.command && <pre>{request.params.command}</pre>}
      {request.params.details != null && !request.params.command && <pre>{JSON.stringify(request.params.details, null, 2)}</pre>}
      {(request.params.permissions || request.params.additionalPermissions) != null &&
        <pre>{JSON.stringify(request.params.permissions ?? request.params.additionalPermissions, null, 2)}</pre>}
    </>}
    {error && <p className="error-text small" role="alert">{error}</p>}
    <div className="settings-actions">
      {(questions || choices.includes('accept')) && <button className="btn btn-primary btn-small" disabled={busy || (!!questions && questions.some(q => !answers[q.id]?.trim()))}>
        {busy ? '正在提交…' : questions ? '提交回答' : '允许这次操作'}
      </button>}
      {(questions || choices.includes('decline')) && <button type="button" className="btn btn-small" disabled={busy}
        onClick={() => { if (questions) setAnswers({}); void respond('decline') }}>{questions ? '跳过' : '拒绝'}</button>}
    </div>
  </form>
}
