import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ImageIntake from '@/components/ImageIntake.vue'

function makeFile(name: string, type = 'image/png', bytes = 4): File {
  return new File([new Uint8Array(bytes)], name, { type })
}

type Wrapper = ReturnType<typeof mountIntake>['wrapper']

function mountIntake(props: Record<string, unknown> = {}) {
  const wrapper = mount(ImageIntake, { props: { files: [], ...props } })
  return { wrapper }
}

async function chooseFiles(wrapper: Wrapper, files: File[]): Promise<void> {
  const input = wrapper.find('input[type="file"]')
  Object.defineProperty(input.element, 'files', { value: files, configurable: true })
  await input.trigger('change')
}

async function dropFiles(wrapper: Wrapper, files: File[]): Promise<void> {
  await wrapper.find('[role="button"]').trigger('drop', { dataTransfer: { files } })
}

function pasteEvent(files: File[], plainText = false): ClipboardEvent {
  const event = new Event('paste', { bubbles: true, cancelable: true }) as ClipboardEvent
  const items = plainText
    ? [{ kind: 'string', type: 'text/plain', getAsFile: () => null }]
    : files.map((file) => ({ kind: 'file', type: file.type, getAsFile: () => file }))
  Object.defineProperty(event, 'clipboardData', { value: { items } })
  return event
}

async function paste(wrapper: Wrapper, event: ClipboardEvent): Promise<void> {
  wrapper.find('[role="button"]').element.dispatchEvent(event)
  await wrapper.vm.$nextTick()
}

/** 最近一次 `update:files` 的值（没触发就是空数组）。 */
function lastEmitted(wrapper: Wrapper): File[] {
  const events = wrapper.emitted('update:files') ?? []
  return (events[events.length - 1]?.[0] as File[]) ?? []
}

function lastError(wrapper: Wrapper): string {
  const events = wrapper.emitted('error') ?? []
  return String(events[events.length - 1]?.[0] ?? '')
}

describe('ImageIntake（S7.17 共享图片入口）', () => {
  it('选择文件后把整批交给 v-model', async () => {
    const { wrapper } = mountIntake()
    await chooseFiles(wrapper, [makeFile('a.png')])
    expect(lastEmitted(wrapper).map((file) => file.name)).toEqual(['a.png'])
  })

  it('拖拽进来的文件同样收下（一次多张）', async () => {
    const { wrapper } = mountIntake({ max: 9 })
    await dropFiles(wrapper, [makeFile('a.png'), makeFile('b.webp', 'image/webp')])
    expect(lastEmitted(wrapper).map((file) => file.name)).toEqual(['a.png', 'b.webp'])
  })

  it('粘贴多张图片：全部收下，没名字的截图补可读名', async () => {
    const { wrapper } = mountIntake({ max: 9 })
    const pasted = [makeFile('', 'image/png'), makeFile('', 'image/jpeg')]
    await paste(wrapper, pasteEvent(pasted))
    expect(lastEmitted(wrapper).map((file) => file.name))
      .toEqual(['粘贴图片-1.png', '粘贴图片-2.jpg'])
  })

  it('粘贴纯文本不拦截（否则会吃掉输入框的正常粘贴）', async () => {
    const { wrapper } = mountIntake()
    const event = pasteEvent([], true)
    await paste(wrapper, event)
    expect(event.defaultPrevented).toBe(false)
    expect(wrapper.emitted('update:files')).toBeUndefined()
  })

  it('超过张数上限：整批拒绝、给明确文案、不截断', async () => {
    const { wrapper } = mountIntake({ max: 2 })
    await chooseFiles(wrapper, [makeFile('a.png'), makeFile('b.png'), makeFile('c.png')])
    expect(wrapper.emitted('update:files')).toBeUndefined()
    expect(lastError(wrapper)).toBe('最多选 2 张图片（这次收到 3 张）')
  })

  it('已有选中项时，再选一批会超上限也整批拒绝（保留原来那批）', async () => {
    const { wrapper } = mountIntake({ files: [makeFile('旧.png')], max: 2 })
    await chooseFiles(wrapper, [makeFile('a.png'), makeFile('b.png')])
    expect(wrapper.emitted('update:files')).toBeUndefined()
    expect(lastError(wrapper)).toContain('最多选 2 张')
  })

  it('单张超体积或类型不在白名单：指明是第几张', async () => {
    const { wrapper } = mountIntake({ maxSizeMb: 1 })
    await chooseFiles(wrapper, [makeFile('a.png'), makeFile('big.png', 'image/png', 1024 * 1024 + 1)])
    expect(lastError(wrapper)).toBe('第 2 张超过 1 MB 上限')

    const other = mountIntake()
    await chooseFiles(other.wrapper, [makeFile('a.gif', 'image/gif')])
    expect(lastError(other.wrapper)).toBe('第 1 张不是 jpg / png / webp 图片')
  })

  it('合计超体积上限：整批拒绝', async () => {
    const { wrapper } = mountIntake({ max: 9, maxTotalMb: 1 })
    await chooseFiles(wrapper, [
      makeFile('a.png', 'image/png', 600 * 1024),
      makeFile('b.png', 'image/png', 600 * 1024),
    ])
    expect(wrapper.emitted('update:files')).toBeUndefined()
    expect(lastError(wrapper)).toContain('合计超过 1 MB 上限')
  })

  it('缩略图可逐张删除；thumbs=false 时不渲染', async () => {
    const { wrapper } = mountIntake({
      files: [makeFile('a.png'), makeFile('b.png')], max: 9,
    })
    expect(wrapper.findAll('.n-tag')).toHaveLength(2)
    await wrapper.findAll('.n-tag__close')[0].trigger('click')
    expect(lastEmitted(wrapper).map((file) => file.name)).toEqual(['b.png'])

    const bare = mountIntake({ files: [makeFile('a.png')], thumbs: false })
    expect(bare.wrapper.findAll('.n-tag')).toHaveLength(0)
  })

  it('校验通过时清掉上一次的提示（error 传空串）', async () => {
    const { wrapper } = mountIntake()
    await chooseFiles(wrapper, [makeFile('a.gif', 'image/gif')])
    await chooseFiles(wrapper, [makeFile('a.png')])
    expect(lastError(wrapper)).toBe('')
  })
})
