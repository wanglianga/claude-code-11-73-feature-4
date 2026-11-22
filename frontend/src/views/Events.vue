<template>
  <div>
    <h2>接入账（追加式双时间事件账）</h2>

    <div class="card">
      <h3>写入事件</h3>
      <div class="form-row">
        <label>来源</label>
        <select v-model="form.source">
          <option>pos</option><option>online</option><option>franchise_offline</option>
          <option>identity_desk</option><option>rule_admin</option>
        </select>
        <label>类型</label>
        <select v-model="form.event_type">
          <option>transaction</option><option>identity_merge</option><option>identity_split</option>
          <option>identity_pending</option><option>identity_revoke</option>
          <option>rule_upsert</option><option>rule_withdraw</option>
        </select>
        <label>业务时间</label>
        <input v-model="form.biz_time" placeholder="2026-02-10T12:00:00Z" style="width:190px" />
        <label>平台接收时间(可空=当前)</label>
        <input v-model="form.ingested_at" placeholder="留空=服务器当前时间" style="width:190px" />
      </div>
      <div class="form-row">
        <label>幂等键</label>
        <input v-model="form.idempotency_key" placeholder="可空=按内容生成" style="width:220px" />
        <label>修订目标事件id</label>
        <input v-model="form.supersedes_id" placeholder="可空" style="width:80px" />
      </div>
      <textarea v-model="payloadText" rows="4" style="width:100%" placeholder='payload JSON，如 {"member_key":"13800000001","store":"S001","category":"OTC","amount":500}'></textarea>
      <div style="margin-top:8px">
        <button @click="submit">写入账本</button>
        <span v-if="msg" class="muted" style="margin-left:10px">{{ msg }}</span>
      </div>
    </div>

    <div class="card">
      <h3>账本流水（{{ events.length }} 条）</h3>
      <div class="form-row">
        <label>类型过滤</label>
        <select v-model="filterType" @change="load">
          <option value="">全部</option>
          <option>transaction</option><option>identity_merge</option><option>identity_split</option>
          <option>identity_pending</option><option>identity_revoke</option>
          <option>rule_upsert</option><option>rule_withdraw</option>
        </select>
        <label>会员键</label>
        <input v-model="filterMember" placeholder="可空" @change="load" />
      </div>
      <div style="overflow:auto">
        <table>
          <thead>
            <tr><th>id</th><th>类型</th><th>来源</th><th>业务时间</th><th>接收时间</th><th>修订链</th><th>幂等键</th><th>来源哈希</th><th>链哈希</th><th>payload</th></tr>
          </thead>
          <tbody>
            <tr v-for="e in events" :key="e.id">
              <td class="mono">{{ e.id }}</td>
              <td><span class="tag">{{ e.event_type }}</span></td>
              <td>{{ e.source }}</td>
              <td class="mono">{{ fmt(e.biz_time) }}</td>
              <td class="mono">{{ fmt(e.ingested_at) }}</td>
              <td class="mono">{{ e.supersedes_id ? '↳ #' + e.supersedes_id : '' }}</td>
              <td class="mono">{{ e.idempotency_key }}</td>
              <td class="mono" :title="e.source_hash">{{ short(e.source_hash) }}</td>
              <td class="mono" :title="e.chain_hash">{{ short(e.chain_hash) }}</td>
              <td class="mono" style="max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" :title="JSON.stringify(e.payload)">{{ JSON.stringify(e.payload) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const events = ref([])
const filterType = ref('')
const filterMember = ref('')
const msg = ref('')
const form = ref({ source: 'pos', event_type: 'transaction', biz_time: '', ingested_at: '', idempotency_key: '', supersedes_id: '' })
const payloadText = ref('{"member_key":"13800000001","store":"S001","category":"OTC","amount":100}')

function fmt(s) { return s ? s.replace('T', ' ').slice(0, 19) : '' }
function short(h) { return h ? h.slice(0, 10) + '…' : '' }

async function load() {
  let q = '/events?'
  if (filterType.value) q += `event_type=${filterType.value}&`
  if (filterMember.value) q += `member=${filterMember.value}`
  events.value = await api.get(q)
}

async function submit() {
  msg.value = ''
  try {
    const body = {
      source: form.value.source,
      event_type: form.value.event_type,
      biz_time: form.value.biz_time,
      payload: JSON.parse(payloadText.value),
    }
    if (form.value.ingested_at) body.ingested_at = form.value.ingested_at
    if (form.value.idempotency_key) body.idempotency_key = form.value.idempotency_key
    if (form.value.supersedes_id) body.supersedes_id = Number(form.value.supersedes_id)
    const r = await api.post('/events', body)
    msg.value = r.created ? `已入账 #${r.event.id}` : `幂等命中已有事件 #${r.event.id}（未重复入账）`
    await load()
  } catch (e) {
    msg.value = '失败：' + e.message
  }
}

onMounted(load)
</script>
