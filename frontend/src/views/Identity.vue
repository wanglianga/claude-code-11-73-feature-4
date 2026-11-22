<template>
  <div>
    <h2>身份关系图（合并 / 拆分 / 待核 / 撤销的有效区间）</h2>

    <div class="card">
      <h3>任意时点身份簇解析</h3>
      <div class="form-row">
        <label>会员</label>
        <select v-model="member">
          <option v-for="m in members" :key="m.member_key" :value="m.member_key">{{ m.member_key }}</option>
        </select>
        <label>业务时点</label>
        <input v-model="bizTime" style="width:200px" />
        <label>系统时点</label>
        <input v-model="sysTime" placeholder="留空=当前" style="width:200px" />
        <button @click="resolve">解析</button>
      </div>
      <p v-if="cluster">
        簇成员：<span v-for="c in cluster.cluster" :key="c" class="tag" style="margin-right:4px">{{ c }}</span>
        <span class="muted" style="margin-left:8px">（{{ cluster.biz_as_of?.slice(0, 10) }} / 系统 {{ cluster.sys_as_of?.slice(0, 10) }}）</span>
      </p>
      <p class="muted">对照：业务 2026-02-28 + 系统 2026-02-28 → {A,B}；业务 2026-02-28 + 系统当前 → {A}（误合并已追溯拆分）。</p>
    </div>

    <div class="card">
      <h3>关系边（双时间区间）</h3>
      <table>
        <thead>
          <tr><th>id</th><th>关系</th><th>双方</th><th>状态</th><th>业务区间</th><th>系统区间</th><th>目标边</th><th>事由</th></tr>
        </thead>
        <tbody>
          <tr v-for="e in edges" :key="e.id">
            <td class="mono">#{{ e.id }}</td>
            <td><span class="tag">{{ e.relation }}</span></td>
            <td class="mono">{{ e.member_a }} ↔ {{ e.member_b }}</td>
            <td><span :class="['tag', e.status === 'active' ? 'ok' : '']">{{ e.status }}</span></td>
            <td class="mono">{{ fmt(e.biz_from) }} → {{ e.biz_to ? fmt(e.biz_to) : '开放' }}</td>
            <td class="mono">{{ fmt(e.sys_from) }} → {{ e.sys_to ? fmt(e.sys_to) : '开放' }}</td>
            <td class="mono">{{ e.target_edge_id ? '#' + e.target_edge_id : '' }}</td>
            <td class="muted">{{ e.reason }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const members = ref([])
const edges = ref([])
const member = ref('13800000001')
const bizTime = ref('2026-02-28T23:59:00Z')
const sysTime = ref('')
const cluster = ref(null)

function fmt(s) { return s ? s.slice(0, 10) : '' }

async function resolve() {
  let q = `/members/${member.value}/cluster?biz_time=${encodeURIComponent(bizTime.value)}`
  if (sysTime.value) q += `&sys_time=${encodeURIComponent(sysTime.value)}`
  cluster.value = await api.get(q)
}

onMounted(async () => {
  members.value = await api.get('/members')
  edges.value = await api.get('/identity/edges')
  await resolve()
})
</script>
