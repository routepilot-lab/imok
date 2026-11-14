'use strict';
'require baseclass';
// Pure display decisions; no network or configuration writes.
return baseclass.extend({
  firstSetup: function ({
    config,
    status,
    registered,
    runtime,
    runtimePending,
    runtimeUnavailable,
    networks,
    registrationCompleted,
    directoryChecked,
    directoryPending,
    directoryExpired,
    now
  }) {
    var fresh = this.fresh(status, now);
    var warp = fresh && status.state === 'running' && status.backend === 'masque';
    var connected =
      config.enabled &&
      warp &&
      config.nonce &&
      status.applied_nonce === config.nonce &&
      status.exit === config.exit &&
      status.runtime_id &&
      status.exit_ip &&
      status.tunnel_status !== 'unknown';
    if (connected) return { stage: 'checks', reason: 'connected' };

    if (!config.enabled && runtimePending) return { stage: 'install', reason: 'runtime_check' };
    if (!config.enabled && runtimeUnavailable && config.backend !== 'worker')
      return { stage: 'install', reason: 'runtime_unavailable' };
    if (
      !config.enabled &&
      runtime &&
      (runtime.installed === false || runtime.recovery_required === true)
    )
      return {
        stage: 'install',
        reason: runtime.runtime_exists || runtime.recovery_required ? 'runtime_review' : 'install'
      };

    if (!config.enabled && registered !== true && runtime?.registration_files_present === false)
      return { stage: 'account', reason: 'registration' };
    if (!config.enabled && Array.isArray(networks) && !networks.length)
      return { stage: 'account', reason: 'catalog_empty' };
    if (!config.enabled && directoryExpired && config.preparing)
      return { stage: 'account', reason: 'catalog_expired' };
    if (!config.enabled && registrationCompleted && !Array.isArray(networks))
      return {
        stage: 'account',
        reason: directoryPending || !directoryChecked ? 'catalog_pending' : 'catalog_failed'
      };

    var hasExit = /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(config.exit_id || '');
    var hasScope =
      (config.scope === 'lan' && config.lans > 0) ||
      (config.scope === 'devices' && config.devices > 0);
    if (!hasExit || !hasScope || !config.nonce) {
      if (registered !== true && !warp && !(hasExit && config.nonce))
        return {
          stage: 'account',
          reason: runtime?.registration_files_present ? 'account_check' : 'registration'
        };
      return {
        stage: 'routing',
        reason: hasExit && config.scope === 'devices' && !hasScope ? 'devices' : 'settings'
      };
    }
    if (
      !config.enabled &&
      Array.isArray(networks) &&
      !networks.some(function (item) {
        return item.id === config.exit_id;
      })
    )
      return { stage: 'routing', reason: 'exit_unavailable' };
    if (!config.enabled)
      return {
        stage: 'power',
        reason: config.preparing || (fresh && status.state === 'preparing') ? 'preparing' : 'off'
      };
    if (fresh && status.attempted_nonce === config.nonce && status.state === 'error')
      return { stage: 'connecting', reason: 'failed' };
    if (fresh && status.applied_nonce === config.nonce && status.state === 'waiting_devices')
      return { stage: 'connecting', reason: 'waiting_devices' };
    if (fresh && status.state === 'running' && status.backend === 'worker')
      return { stage: 'connecting', reason: 'backup' };
    return { stage: 'connecting', reason: fresh ? 'waiting' : 'unavailable' };
  },

  overviewSummary: function (saved, status, website, now) {
    if (!saved.enabled) return { state: 'off', reason: 'disabled', page: 'overview#imok-power' };
    if (saved.nonce && status?.state === 'disabled' && status.applied_nonce !== saved.nonce)
      return { state: 'unverified', reason: 'starting', page: 'overview#imok-power' };
    if (!this.fresh(status, now))
      return { state: 'unavailable', reason: 'stale', page: 'cloudflare/dns' };
    if (status.state !== 'running')
      return {
        state: 'unavailable',
        reason: status.state === 'waiting_devices' ? 'devices' : 'tunnel',
        page: status.state === 'waiting_devices' ? 'cloudflare/devices' : 'cloudflare/dns'
      };
    if (status.egress_guard === 'blocking' || status.egress_guard === 'unverified')
      return { state: 'partial', reason: 'guard', page: 'cloudflare/devices' };
    if (saved.nonce && saved.nonce !== status.applied_nonce)
      return { state: 'partial', reason: 'apply', page: 'cloudflare/routing' };
    if (status.tunnel_status === 'unknown')
      return { state: 'partial', reason: 'unknown_tunnel', page: 'runtime' };
    var page = 'overview#imok-network-diagnostics';
    if (website === 'success') return { state: 'normal', reason: 'verified', page: page };
    if (website === 'error') return { state: 'partial', reason: 'website', page: page };
    if (website === 'warning') return { state: 'partial', reason: 'restricted', page: page };
    return {
      state: 'unverified',
      reason: website === 'busy' ? 'testing' : 'not_tested',
      page: page
    };
  },

  workerJobKey: function (job) {
    return job ? String(job.started_at || '') + '|' + String(job.action || '') : 'none';
  },
  workerJobProgress: function (job, pending, now, kind, total) {
    if (!job && !pending)
      return {
        state: 'idle',
        phase: '',
        elapsed: 0,
        completed: 0,
        total: total || 0,
        matched: false
      };
    var limit = kind === 'backup' ? 100 : 300,
      start = pending ? pending.started : Number(job && job.started_at) * 1000;
    if (!Number.isFinite(start)) start = now;
    var elapsed = Math.max(0, Math.floor((now - start) / 1000));
    var matched =
      !!job &&
      (!pending ||
        (this.workerJobKey(job) !== pending.baseline &&
          Number(job.started_at) * 1000 >= pending.started - 5000 &&
          (!pending.action || job.action === pending.action)));
    if (!matched)
      return {
        state: elapsed >= limit ? 'unknown' : 'busy',
        phase: 'awaiting',
        elapsed: elapsed,
        completed: 0,
        total: total || 0,
        matched: false
      };
    var count = Array.isArray(job.rows)
      ? job.rows.length
      : Array.isArray(job.results)
        ? job.results.length
        : 0;
    var state = job.state,
      phase = job.phase || '',
      result = {
        elapsed: elapsed,
        completed: count,
        total: Number(job.total) || total || 0,
        phase: phase,
        matched: true,
        message: job.message || '',
        finished_at: job.finished_at
      };
    result.state =
      ['queued', 'running'].indexOf(state) >= 0
        ? now / 1000 - Number(job.started_at) >= limit
          ? 'unknown'
          : 'busy'
        : ['failed', 'unreachable', 'deployment_error'].indexOf(state) >= 0
          ? 'error'
          : state === 'conflict'
            ? 'warning'
            : ['waiting_dns', 'dns_unavailable', 'tls_unavailable', 'access_restricted'].indexOf(
                  state
                ) >= 0
              ? 'waiting'
              : ['ready', 'verified', 'available', 'done'].indexOf(state) >= 0
                ? 'success'
                : 'idle';
    if (kind === 'cloud' && job.action === 'deploy') {
      var index = { connecting: 0, checking: 0, uploading: 1, binding: 2, waiting_dns: 3 }[phase];
      result.total = 4;
      result.completed =
        result.state === 'success'
          ? 4
          : result.state === 'waiting'
            ? 3
            : Number.isFinite(index)
              ? index
              : 0;
    }
    return result;
  },

  diagnosticValid: function (test) {
    if (
      !test ||
      typeof test !== 'object' ||
      !['running', 'done'].includes(test.state) ||
      !Array.isArray(test.results) ||
      test.results.length > 5
    )
      return false;
    var seen = {};
    return test.results.every(function (row) {
      if (
        !row ||
        typeof row !== 'object' ||
        !['google', 'youtube', 'github', 'taobao', 'baidu'].includes(row.id) ||
        seen[row.id] ||
        !['pending', 'ok', 'failed', 'restricted'].includes(row.state)
      )
        return false;
      seen[row.id] = true;
      return true;
    });
  },
  wizardTestToken: function (test) {
    if (!this.diagnosticValid(test)) return 'none';
    return JSON.stringify([
      test.started_at,
      test.finished_at,
      test.state,
      test.exit,
      test.exit_ip,
      test.runtime_id,
      test.generation,
      test.applied_nonce,
      (test.results || [])
        .map(function (r) {
          return [r.id, r.state, r.http, r.ms];
        })
        .sort(function (a, b) {
          return String(a[0]).localeCompare(String(b[0]));
        })
    ]);
  },
  wizardProxyVerification: function (test, status, config, now) {
    var result = this.wizardVerification(test, status, config, now);
    if (['success', 'error', 'warning'].indexOf(result) < 0) return result;
    var proxy = test.results.filter(function (r) {
      return ['google', 'youtube', 'github'].indexOf(r.id) >= 0;
    });
    return proxy.some(function (r) {
      return r.state === 'failed';
    })
      ? 'error'
      : proxy.some(function (r) {
            return r.state === 'restricted';
          })
        ? 'warning'
        : proxy.every(function (r) {
              return r.state === 'ok';
            })
          ? 'success'
          : 'stale';
  },
  wizardVerification: function (test, status, config, now) {
    if (!this.fresh(status, now) || status.state !== 'running') return 'pending';
    var backend = config.backend || 'masque',
      exit = backend === 'worker' ? 'worker' : config.exit;
    if (
      status.applied_nonce !== config.nonce ||
      (status.backend || 'masque') !== backend ||
      status.exit !== exit
    )
      return 'stale';
    if (!this.diagnosticValid(test) || this.diagnosticStale(test, status, now)) return 'stale';
    if (this.diagnosticRunning(test, now)) return 'busy';
    var ids = ['google', 'youtube', 'github', 'taobao', 'baidu'];
    if (test.state !== 'done' || !Array.isArray(test.results) || test.results.length !== ids.length)
      return 'stale';
    if (
      !ids.every(function (id) {
        return (
          test.results.filter(function (r) {
            return r.id === id;
          }).length === 1
        );
      })
    )
      return 'stale';
    if (
      test.results.some(function (r) {
        return r.state === 'failed';
      })
    )
      return 'error';
    if (
      test.results.some(function (r) {
        return r.state === 'restricted';
      })
    )
      return 'warning';
    return test.results.every(function (r) {
      return r.state === 'ok';
    })
      ? 'success'
      : 'stale';
  },
  wizardSteps: function (config, catalog, status, test, actions, now, registeredHere) {
    actions = actions || {};
    var readAge = now - Number(catalog.read_at),
      directory =
        !!catalog.available && Number.isFinite(readAge) && readAge >= -5000 && readAge <= 60000;
    var ready = this.fresh(status, now) && ['preparing', 'running'].indexOf(status.state) >= 0;
    var states = [
      {
        id: 'org',
        state:
          directory || /^[a-zA-Z0-9-]{1,63}$/.test(config.organization || '')
            ? 'success'
            : 'pending',
        reason: directory ? 'reuse' : 'organization'
      },
      {
        id: 'prepare',
        state: ready || directory ? 'success' : 'pending',
        reason: directory ? 'reuse' : 'environment'
      },
      {
        id: 'auth',
        state: directory || registeredHere ? 'success' : 'pending',
        reason: catalog.last_success_at ? 'access_previous' : 'access'
      }
    ];
    var token = this.wizardTestToken(test);
    states.forEach(function (step) {
      var action = actions[step.id];
      if (action && action.state === 'error') step.state = 'error';
      else if (
        action &&
        action.state === 'busy' &&
        (step.id !== 'verify' || action.baseline === token || (test && test.state === 'running'))
      )
        step.state = 'busy';
    });
    return states;
  },
  dnsState: function (data, status, now) {
    if (!this.fresh(status, now) || status.state !== 'running') return 'inactive';
    if (!data || data.schema !== 1 || !Array.isArray(data.policies)) return 'unavailable';
    var age = now / 1000 - Number(data.time);
    if (
      !Number.isFinite(age) ||
      age < -1 ||
      age > 5 ||
      !data.runtime_id ||
      !data.generation ||
      !data.process_instance ||
      data.runtime_id !== status.runtime_id ||
      data.generation !== status.generation
    )
      return 'stale';
    if (
      data.state !== 'running' ||
      !data.policies.length ||
      data.policies.length > 8 ||
      !data.upstreams ||
      !data.policies.every(function (p) {
        return (
          p &&
          typeof p === 'object' &&
          ['cache_entries', 'cache_hits', 'requests', 'errors'].every(function (k) {
            return Number.isSafeInteger(p[k]) && p[k] >= 0;
          })
        );
      })
    )
      return 'unavailable';
    return 'running';
  },
  dnsTestValid: function (test) {
    return (
      !!test &&
      typeof test === 'object' &&
      ['running', 'done'].includes(test.state) &&
      Array.isArray(test.results) &&
      test.results.length <= 32 &&
      test.results.every(function (row) {
        return (
          !!row &&
          typeof row === 'object' &&
          Number.isSafeInteger(row.policy) &&
          ['domestic', 'proxy'].includes(row.target) &&
          ['udp', 'tcp'].includes(row.protocol)
        );
      })
    );
  },
  dnsTestState: function (test, status, dns, now) {
    if (!test) return this.dnsState(dns, status, now) === 'running' ? 'pending' : 'unavailable';
    if (
      !this.dnsTestValid(test) ||
      this.dnsState(dns, status, now) !== 'running' ||
      this.diagnosticStale(test, status, now) ||
      test.process_instance !== dns.process_instance
    )
      return 'stale';
    var age = now / 1000 - Number(test.started_at);
    if (test.state === 'running')
      return Number.isFinite(age) && age >= -5 && age < 50 ? 'busy' : 'stale';
    var finishedAge = now / 1000 - Number(test.finished_at);
    if (
      test.state !== 'done' ||
      !Number.isFinite(finishedAge) ||
      finishedAge < -5 ||
      finishedAge > 300 ||
      !Array.isArray(test.results)
    )
      return 'stale';
    var keys = {};
    test.results.forEach(function (r) {
      keys[r.policy + '/' + r.target + '/' + r.protocol] =
        (keys[r.policy + '/' + r.target + '/' + r.protocol] || 0) + 1;
    });
    if (
      test.results.length !== dns.policies.length * 4 ||
      !dns.policies.every(function (p) {
        return ['domestic', 'proxy'].every(function (t) {
          return ['udp', 'tcp'].every(function (proto) {
            return keys[p.index + '/' + t + '/' + proto] === 1;
          });
        });
      })
    )
      return 'stale';
    if (
      test.results.some(function (r) {
        return r.state === 'failed';
      })
    )
      return 'error';
    return test.results.every(function (r) {
      return r.state === 'ok';
    })
      ? 'success'
      : 'warning';
  },
  receiveState: function (data, status, now) {
    if (!data || data.schema !== 1) return 'unavailable';
    var age = now / 1000 - Number(data.time);
    if (!Number.isFinite(age) || age < -1 || age > 5) return 'stale';
    if (data.state !== 'observing')
      return ['inactive', 'no_sockets', 'unavailable'].indexOf(data.state) >= 0
        ? data.state
        : 'unavailable';
    if (
      !this.fresh(status, now) ||
      status.state !== 'running' ||
      !data.runtime_id ||
      data.runtime_id !== status.runtime_id ||
      !data.generation ||
      data.generation !== status.generation
    )
      return 'stale';
    if (
      ![
        'observed_drops',
        'queue_now',
        'peak_queue',
        'socket_count',
        'known_ingress_count',
        'other_public_udp_count'
      ].every(function (k) {
        return Number.isSafeInteger(data[k]) && data[k] >= 0;
      })
    )
      return 'unavailable';
    return data.coverage === 'complete'
      ? 'observing'
      : data.coverage === 'partial'
        ? 'partial'
        : 'unavailable';
  },
  diagnosticRunning: function (state, now) {
    var age = now / 1000 - Number(state && state.started_at);
    return !!state && state.state === 'running' && Number.isFinite(age) && age >= -5 && age < 30;
  },
  updateRunning: function (state, now) {
    var age = now / 1000 - Number(state && state.started_at);
    return !!state && state.state === 'running' && Number.isFinite(age) && age >= -5 && age < 180;
  },
  fresh: function (status, now) {
    var age = now / 1000 - Number(status && status.time);
    return !!status && Number.isFinite(age) && age >= -5 && age <= 15;
  },
  diagnosticStale: function (test, status, now) {
    var age = now / 1000 - Number(test && test.finished_at);
    return (
      !test ||
      test.stale ||
      (test.state === 'done' && (!Number.isFinite(age) || age < -5 || age > 300)) ||
      !this.fresh(status, now) ||
      status.state !== 'running' ||
      !test.generation ||
      test.generation !== status.generation ||
      !test.runtime_id ||
      test.runtime_id !== status.runtime_id ||
      (test.backend != null && test.backend !== status.backend) ||
      test.exit !== status.exit ||
      test.exit_ip !== status.exit_ip ||
      test.applied_nonce !== status.applied_nonce
    );
  }
});
