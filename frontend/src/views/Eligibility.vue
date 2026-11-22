<template>
  <div>
    <h2>资格查询（任意双时点）</h2>

    <div class="card">
      <div class="form-row">
        <label>会员</label>
        <select v-model="member">
          <option v-for="m in members" :key="m.member_key" :value="m.member_key">
            {{ m.member_key }} {{ m.display_name ? '· ' + m.display_name : '' }}
          </option>
        </select>
        <label>业务时点</label>
        <input v-model="bizTime" placeholder="2026-02-28T23:59:00Z" style="width:200px" />
        <label>查询口径</label>
        <select v-model="semantics">
          <option value="AS_OF">AS_OF · 当时可见</option>
          <option value="AS_KNOWN_NOW">AS_KNOWN_NOW · 事后补全</option>
          <option value="DELTA">DELTA · 二者差异</option>
        </select>
        <label v-if="semantics !== 'AS_KNOWN_NOW'">系统时点</label>
        <input v-if="semantics !== 'AS_KNOWN_NOW'" v-model="sysTime" placeholder="留空=业务时点" style="width:200px" />
        <button @click="query">查询</button>
      </div>
      <p class="muted">
        提示：试试 2026-01-31（离线补传改判）、2026-02-28（误合并拆分 + 规则追溯 + 部分撤回）、2026-03-31（并发修订后 v3 生效）。
      </p>
    </div>

    <template v-if="result">
      <div v-if="result.semantics !== 'DELTA'" class="grid cols-2">
        <div class="card">
          <h3>
            结论
            <span :class="['tag', result.tier]" style="font-size:16px">{{ result.tier }}</span>
            <span class="muted">窗口消费 ¥{{ result.total_spend }}</span>
          </h3>
          <p>身份簇：<span v-for="c in result.cluster" :key="c" class="tag" style="margin-right:4px">{{ c }}</span></p>
          <p class="muted">业务时点 {{ result.biz_as_of }} ｜ 系统时点 {{ result.sys_as_of }} ｜ 口径 {{ result.semantics }}</p>
          <p v-if="result.replaced_events?.length" class="muted">
            修订留痕：{{ result.replaced_events.map(r => `#${r.event_id}→#${r.replaced_by}`).join('，') }}
          </p>
        </div>
        <div class="card">
          <h3>等级解释（为何获得/失去资格）</h3>
          <table>
            <thead><tr><th>规则版本</th><th>门店范围</th><th>评估窗口</th><th>消费</th><th>等级</th><th>计入事件</th></tr></thead>
            <tbody>
              <tr v-for="(s, i) in result.scopes" :key="i">
                <td>{{ s.rule_id }} v{{ s.rule_version }}</td>
                <td>{{ s.stores.join(', ') }}</td>
                <td class="mono">{{ s.window_from.slice(0, 10) }} ~ {{ s.window_to.slice(0, 10) }}</td>
                <td>¥{{ s.spend }}</td>
                <td><span :class="['tag', s.tier]">{{ s.tier }}</span></td>
                <td class="mono">{{ s.counted_event_ids.map(x => '#' + x).join(' ') }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div v-else class="card">
        <h3>两种口径差异（{{ result.biz_as_of?.slice(0, 10) }}）</h3>
        <div class="grid cols-2">
          <div>
            <h4>当时可见</h4>
            <p><span :class="['tag', result.as_of.tier]">{{ result.as_of.tier }}</span> ¥{{ result.as_of.total_spend }}</p>
            <p class="muted">簇：{{ result.as_of.cluster.join(', ') }}</p>
          </div>
          <div>
            <h4>事后补全</h4>
            <p><span :class="['tag', result.as_known_now.tier]">{{ result.as_known_now.tier }}</span> ¥{{ result.as_known_now.total_spend }}</p>
            <p class="muted">簇：{{ result.as_known_now.cluster.join(', ') }}</p>
          </div>
        </div>
        <p>
          <span v-if="result.tier_changed" class="tag bad">等级变化：{{ result.as_of.tier }} → {{ result.as_known_now.tier }}</span>
          <span v-else class="tag ok">等级一致</span>
          <span class="muted" style="margin-left:10px">消费差 ¥{{ result.spend_delta }}</span>
        </p>
      </div>

      <div class="card">
        <h3>等级时间线（系统时点 = 当前）</h3>
        <div class="timeline">
          <div v-for="p in timeline" :key="p.biz_time" :class="['bar', p.tier]"
               :style="{ height: barHeight(p.tier) }"
               :data-tip="`${p.biz_time.slice(0, 10)}  ${p.tier}  ¥${p.spend}`"></div>
        </div>
        <p class="muted">2026-01 ~ 2026-05，柱高=等级（NONE/SILVER/GOLD），悬停查看明细</p>
      </div>
    </template>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const members = ref([])
const member = ref('13800000001')
const bizTime = ref('2026-02-28T23:59:00Z')
const sysTime = ref('')
const semantics = ref('DELTA')
const result = ref(null)
const timeline = ref([])

function barHeight(t) {
  return { NONE: '18%', SILVER: '45%', GOLD: '75%', PLATINUM: '100%' }[t] || '18%'
}

async function query() {
  let q = `/members/${member.value}/eligibility?biz_time=${encodeURIComponent(bizTime.value)}&semantics=${semantics.value}`
  if (sysTime.value) q += `&sys_time=${encodeURIComponent(sysTime.value)}`
  result.value = await api.get(q)
  timeline.value = await api.get(
    `/members/${member.value}/timeline?biz_from=2026-01-01T00:00:00Z&biz_to=2026-05-01T00:00:00Z&points=24`
  )
}

onMounted(async () => {
  members.value = await api.get('/members')
  await query()
})
</script>
