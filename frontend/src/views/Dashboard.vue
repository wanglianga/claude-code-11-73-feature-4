<template>
  <div>
    <h2>总览</h2>
    <div class="grid cols-4">
      <div class="stat"><div class="num">{{ ov.events }}</div><div class="label">接入账事件</div></div>
      <div class="stat"><div class="num">{{ ov.members }}</div><div class="label">会员</div></div>
      <div class="stat"><div class="num">{{ ov.identity_edges }}</div><div class="label">身份关系边</div></div>
      <div class="stat"><div class="num">{{ ov.rule_versions + ov.rule_withdrawals }}</div><div class="label">规则版本/撤回</div></div>
      <div class="stat"><div class="num">{{ ov.publications }}</div><div class="label">发布快照</div></div>
      <div class="stat"><div class="num">{{ ov.diffs }}</div><div class="label">差异归因</div></div>
      <div class="stat"><div class="num">{{ ov.impacts }}</div><div class="label">下游影响</div></div>
      <div class="stat">
        <div class="num">
          <span v-if="ov.ledger" :class="['tag', ov.ledger.ok ? 'ok' : 'bad']">
            {{ ov.ledger.ok ? '完整' : '断裂' }}
          </span>
        </div>
        <div class="label">账本链式哈希（{{ ov.ledger?.checked ?? 0 }} 条）</div>
      </div>
      <div class="stat"><div class="num">{{ ov.experiments ?? 0 }}</div><div class="label">规则实验</div></div>
      <div class="stat"><div class="num">{{ ov.experiment_versions ?? 0 }}</div><div class="label">实验版本</div></div>
      <div class="stat"><div class="num">{{ ov.snapshots ?? 0 }}</div><div class="label">样本快照</div></div>
      <div class="stat"><div class="num">{{ ov.shadow_runs ?? 0 }}</div><div class="label">影子分群运行</div></div>
      <div class="stat"><div class="num">{{ ov.budget_attributions ?? 0 }}</div><div class="label">预算归因行</div></div>
      <div class="stat"><div class="num">{{ ov.release_gates ?? 0 }}</div><div class="label">发布门禁</div></div>
    </div>

    <div class="grid cols-2" style="margin-top:16px">
      <div class="card">
        <h3>时间语义目录（查询口径）</h3>
        <table>
          <thead><tr><th>口径</th><th>名称</th><th>业务时间</th><th>系统时间</th><th>说明</th></tr></thead>
          <tbody>
            <tr v-for="s in semantics" :key="s.code">
              <td><span class="tag">{{ s.code }}</span></td>
              <td>{{ s.name }}</td>
              <td>{{ s.biz_policy }}</td>
              <td>{{ s.sys_policy }}</td>
              <td class="muted">{{ s.description }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="card">
        <h3>架构与操作</h3>
        <p class="muted">
          采用「追加式双时间事件账 + 可重建投影」：events 账为唯一事实来源（追加-only，链式哈希防篡改），
          身份图 / 规则版本 / 名录均为可重建投影。更正以新事件 + 修订链追加，绝不以当前主档覆盖历史。
        </p>
        <div class="form-row">
          <button @click="doSeed">灌入演示场景</button>
          <button class="ghost" @click="doRebuild">重建投影</button>
          <button class="ghost" @click="doReset">清空业务数据</button>
        </div>
        <pre v-if="result">{{ result }}</pre>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const ov = ref({})
const semantics = ref([])
const result = ref('')

async function load() {
  ov.value = await api.get('/overview')
  semantics.value = await api.get('/semantics')
}
async function doSeed() {
  result.value = JSON.stringify(await api.post('/seed'), null, 2)
  await load()
}
async function doRebuild() {
  result.value = JSON.stringify(await api.post('/admin/rebuild'), null, 2)
  await load()
}
async function doReset() {
  if (!confirm('确认清空全部业务数据？（账号与语义目录保留）')) return
  await api.post('/admin/reset')
  result.value = '已清空'
  await load()
}
onMounted(load)
</script>
