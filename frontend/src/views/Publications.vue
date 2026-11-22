<template>
  <div>
    <h2>发布库 · 差异归因 · 下游影响</h2>

    <div class="card">
      <h3>新建发布（冻结证据水位 / 身份图版本 / 规则集合版本）</h3>
      <div class="form-row">
        <input v-model="pubForm.name" placeholder="名称，如 2026-02月末·当时可见" style="width:240px" />
        <label>业务截止</label>
        <input v-model="pubForm.biz_as_of" style="width:200px" placeholder="2026-02-28T23:59:00Z" />
        <label>系统水位</label>
        <input v-model="pubForm.sys_as_of" style="width:200px" placeholder="2026-02-28T23:59:00Z" />
        <select v-model="pubForm.semantics_code">
          <option>AS_OF</option><option>AS_KNOWN_NOW</option>
        </select>
        <button @click="createPub">冻结发布</button>
      </div>
      <p class="muted">推荐对照组：P1(02-28/02-28) 当时可见 → P2(02-28/03-31) 3月末水位 → P3(02-28/当前) 事后补全。</p>
    </div>

    <div class="card">
      <h3>发布列表</h3>
      <table>
        <thead>
          <tr><th>id</th><th>名称</th><th>业务截止</th><th>系统水位</th><th>证据水位</th><th>身份图版本</th><th>规则集版本</th><th>口径</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="p in pubs" :key="p.id">
            <td class="mono">#{{ p.id }}</td>
            <td>{{ p.name }}</td>
            <td class="mono">{{ p.biz_as_of.slice(0, 10) }}</td>
            <td class="mono">{{ p.sys_as_of.slice(0, 10) }}</td>
            <td class="mono">event#{{ p.watermark_event_id }}</td>
            <td class="mono">event#{{ p.identity_graph_version }}</td>
            <td class="mono">event#{{ p.rule_set_version }}</td>
            <td><span class="tag">{{ p.semantics_code }}</span></td>
            <td><button class="ghost" @click="viewPub(p.id)">明细</button></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="pubDetail" class="card">
      <h3>发布 #{{ pubDetail.id }} 资格快照（{{ pubDetail.rows.length }} 名会员）</h3>
      <table>
        <thead><tr><th>会员</th><th>等级</th><th>窗口消费</th><th>身份簇</th><th>规则版本</th></tr></thead>
        <tbody>
          <tr v-for="r in pubDetail.rows" :key="r.member_key">
            <td class="mono">{{ r.member_key }}</td>
            <td><span :class="['tag', r.tier]">{{ r.tier }}</span></td>
            <td>¥{{ r.spend }}</td>
            <td class="mono">{{ r.detail.cluster.join(', ') }}</td>
            <td class="mono">{{ r.detail.scopes.map(s => 'v' + s.rule_version).join(', ') }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="card">
      <h3>差异归因（比较两个发布）</h3>
      <div class="form-row">
        <select v-model="diffA"><option v-for="p in pubs" :key="p.id" :value="p.id">#{{ p.id }} {{ p.name }}</option></select>
        <span class="arrow">→</span>
        <select v-model="diffB"><option v-for="p in pubs" :key="p.id" :value="p.id">#{{ p.id }} {{ p.name }}</option></select>
        <button @click="createDiff" :disabled="!diffA || !diffB || diffA === diffB">计算差异与影响</button>
      </div>

      <template v-if="diff">
        <h4>资格变化（{{ diff.items.length }} 名会员）</h4>
        <table>
          <thead><tr><th>会员</th><th>变化前</th><th></th><th>变化后</th><th>归因事件</th></tr></thead>
          <tbody>
            <tr v-for="i in diff.items" :key="i.member_key">
              <td class="mono">{{ i.member_key }}</td>
              <td><span :class="['tag', i.before.tier]">{{ i.before.tier }}</span> ¥{{ i.before.spend }}</td>
              <td class="arrow">→</td>
              <td><span :class="['tag', i.after.tier]">{{ i.after.tier }}</span> ¥{{ i.after.spend }}</td>
              <td>
                <span v-for="(c, idx) in i.causes" :key="idx" class="cause" :title="c.detail">
                  {{ c.cause }}#{{ c.event_id }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>

        <h4 style="margin-top:16px">下游影响（营销名单 / 门店预算 / 训练样本）</h4>
        <table>
          <thead><tr><th>维度</th><th>下游实体</th><th>会员</th><th>变化类型</th><th>前</th><th>后</th><th>归因事件</th></tr></thead>
          <tbody>
            <tr v-for="x in diff.impacts" :key="x.id">
              <td><span class="tag">{{ x.dimension }}</span></td>
              <td class="mono">{{ x.entity }}</td>
              <td class="mono">{{ x.member_key }}</td>
              <td>{{ x.change_type }}</td>
              <td class="mono">{{ JSON.stringify(x.before) }}</td>
              <td class="mono">{{ JSON.stringify(x.after) }}</td>
              <td class="mono">{{ (x.attributed_event_ids || []).map(e => '#' + e).join(' ') }}</td>
            </tr>
          </tbody>
        </table>
      </template>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const pubs = ref([])
const pubDetail = ref(null)
const pubForm = ref({ name: '', biz_as_of: '2026-02-28T23:59:00Z', sys_as_of: '2026-02-28T23:59:00Z', semantics_code: 'AS_OF' })
const diffA = ref(null)
const diffB = ref(null)
const diff = ref(null)

async function load() {
  pubs.value = await api.get('/publications')
  if (pubs.value.length >= 2) {
    diffA.value = pubs.value[0].id
    diffB.value = pubs.value[pubs.value.length - 1].id
  }
}
async function createPub() {
  await api.post('/publications', pubForm.value)
  pubForm.value.name = ''
  await load()
}
async function viewPub(id) {
  pubDetail.value = await api.get(`/publications/${id}`)
}
async function createDiff() {
  const d = await api.post('/diffs', { pub_a: diffA.value, pub_b: diffB.value })
  diff.value = await api.get(`/diffs/${d.id}`)
}
onMounted(load)
</script>
