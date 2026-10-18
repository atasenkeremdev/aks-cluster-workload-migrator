"""Extract target references without exposing their content."""


def references(obj):
    spec = obj.get('spec', {})
    template = spec.get('jobTemplate', {}).get('spec', {}).get('template', {}) if obj['kind'] == 'CronJob' else spec.get('template', {})
    pod = template.get('spec', {})
    refs = set()
    def add(kind, name):
        if name:
            refs.add((kind, name))
    add('serviceaccount', pod.get('serviceAccountName'))
    for ref in pod.get('imagePullSecrets', []) or []:
        add('secret', ref.get('name'))
    for container in (pod.get('containers', []) or []) + (pod.get('initContainers', []) or []):
        for env in container.get('env', []) or []:
            value = env.get('valueFrom', {}) or {}
            for field, kind in [('secretKeyRef', 'secret'), ('configMapKeyRef', 'configmap')]:
                ref = value.get(field, {}) or {}
                if not ref.get('optional'):
                    add(kind, ref.get('name'))
        for env in container.get('envFrom', []) or []:
            for field, kind in [('secretRef', 'secret'), ('configMapRef', 'configmap')]:
                ref = env.get(field, {}) or {}
                if not ref.get('optional'):
                    add(kind, ref.get('name'))
    def volume_refs(volume):
        for field, kind, key in [('secret', 'secret', 'secretName'), ('configMap', 'configmap', 'name'), ('persistentVolumeClaim', 'persistentvolumeclaim', 'claimName')]:
            ref = volume.get(field, {}) or {}
            if not ref.get('optional'):
                add(kind, ref.get(key))
    for volume in pod.get('volumes', []) or []:
        volume_refs(volume)
        for source in (volume.get('projected', {}) or {}).get('sources', []) or []:
            for field, kind in [('secret', 'secret'), ('configMap', 'configmap')]:
                ref = source.get(field, {}) or {}
                if not ref.get('optional'):
                    add(kind, ref.get('name'))
    return sorted(refs)
