pipeline {
    agent { label "${params.AGENT_LABEL ?: 'docker'}" }
    options {
        skipDefaultCheckout(true)
        disableConcurrentBuilds()
        timestamps()
        timeout(time: 60, unit: 'MINUTES')
    }
    parameters {
        string(name: 'AGENT_LABEL', defaultValue: 'docker', description: 'Linux Jenkins agent label with Docker daemon and Git access')
        string(name: 'REGISTRY_HOST', defaultValue: 'registry.internal.example:5000', description: 'Registry hostname[:port], without https://')
        string(name: 'IMAGE_REPOSITORY', defaultValue: 'platform/aks-cluster-workload-migrator', description: 'Image repository path inside the registry')
        string(name: 'REGISTRY_CREDENTIALS_ID', defaultValue: 'internal-registry', description: 'Jenkins Username with password credential ID')
        string(name: 'BUILD_CA_CREDENTIALS_ID', defaultValue: '', description: 'Optional Jenkins Secret file credential containing the trusted build proxy CA bundle')
        string(name: 'IMAGE_TAG', defaultValue: '', description: 'Optional tag; defaults to build number and commit SHA')
        string(name: 'PYTHON_IMAGE', defaultValue: 'python:3.12-slim-bookworm', description: 'Python base image; may point at your trusted internal mirror or digest')
        string(name: 'KUBECTL_VERSION', defaultValue: 'v1.34.1', description: 'Choose a kubectl version within one minor of BOTH AKS API servers')
        string(name: 'KUBELOGIN_VERSION', defaultValue: 'v0.2.22', description: 'Pinned Azure kubelogin release')
        string(name: 'AZURE_CLI_VERSION', defaultValue: '2.91.0', description: 'Pinned Azure CLI package version')
    }
    environment {
        DOCKER_BUILDKIT = '1'
    }
    stages {
        stage('Checkout and validate') {
            steps {
                checkout scm
                script {
                    if (!(params.REGISTRY_HOST ==~ /[a-zA-Z0-9][a-zA-Z0-9.-]*(?::[0-9]{1,5})?/)) {
                        error('REGISTRY_HOST must be a hostname[:port], without a URL scheme or path.')
                    }
                    if (!(params.IMAGE_REPOSITORY ==~ /[a-z0-9]+(?:[._-][a-z0-9]+)*(?:\/[a-z0-9]+(?:[._-][a-z0-9]+)*)*/)) {
                        error('IMAGE_REPOSITORY must be a lowercase Docker repository path.')
                    }
                    if (!(params.PYTHON_IMAGE ==~ /[a-zA-Z0-9][a-zA-Z0-9.:\/@_-]*/)) {
                        error('PYTHON_IMAGE must be a Docker image reference.')
                    }
                    for (name in ['KUBECTL_VERSION', 'KUBELOGIN_VERSION']) {
                        if (!(params[name] ==~ /v[0-9]+\.[0-9]+\.[0-9]+/)) { error("Invalid ${name}.") }
                    }
                    if (!(params.AZURE_CLI_VERSION ==~ /[0-9]+\.[0-9]+\.[0-9]+/)) { error('Invalid AZURE_CLI_VERSION.') }
                    def commit = sh(returnStdout: true, script: 'git rev-parse --short=12 HEAD').trim()
                    def tag = params.IMAGE_TAG.trim() ?: "${env.BUILD_NUMBER}-${commit}"
                    if (!(tag ==~ /[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}/)) { error('Invalid IMAGE_TAG.') }
                    env.IMAGE_REF = "${params.REGISTRY_HOST}/${params.IMAGE_REPOSITORY}:${tag}"
                    env.MIGRATOR_DOCKER_CONFIG = sh(returnStdout: true, script: 'mktemp -d /tmp/migrator-docker-config.XXXXXX').trim()
                    env.DOCKER_CONFIG = env.MIGRATOR_DOCKER_CONFIG
                }
                sh 'docker version'
            }
        }
        stage('Registry login') {
            steps {
                withCredentials([usernamePassword(credentialsId: params.REGISTRY_CREDENTIALS_ID,
                    usernameVariable: 'REGISTRY_USERNAME', passwordVariable: 'REGISTRY_PASSWORD')]) {
                    sh '''
                        set +x
                        set -eu
                        printf '%s' "$REGISTRY_PASSWORD" | docker login "$REGISTRY_HOST" --username "$REGISTRY_USERNAME" --password-stdin
                    '''
                }
            }
        }
        stage('Build and unit tests') {
            steps {
                script {
                    def buildCommands = '''
                        set -eu
                        set --
                        if [ -n "${BUILD_CA_FILE:-}" ]; then set -- --secret "id=build_ca,src=$BUILD_CA_FILE"; fi
                        docker build "$@" --pull --target test \
                            --build-arg HTTP_PROXY --build-arg HTTPS_PROXY --build-arg NO_PROXY \
                            --build-arg http_proxy --build-arg https_proxy --build-arg no_proxy \
                            --build-arg PYTHON_IMAGE="$PYTHON_IMAGE" \
                            --build-arg KUBECTL_VERSION="$KUBECTL_VERSION" \
                            --build-arg KUBELOGIN_VERSION="$KUBELOGIN_VERSION" \
                            --build-arg AZURE_CLI_VERSION="$AZURE_CLI_VERSION" .
                        docker build "$@" --target release --tag "$IMAGE_REF" \
                            --build-arg HTTP_PROXY --build-arg HTTPS_PROXY --build-arg NO_PROXY \
                            --build-arg http_proxy --build-arg https_proxy --build-arg no_proxy \
                            --build-arg PYTHON_IMAGE="$PYTHON_IMAGE" \
                            --build-arg KUBECTL_VERSION="$KUBECTL_VERSION" \
                            --build-arg KUBELOGIN_VERSION="$KUBELOGIN_VERSION" \
                            --build-arg AZURE_CLI_VERSION="$AZURE_CLI_VERSION" .
                    '''
                    if (params.BUILD_CA_CREDENTIALS_ID.trim()) {
                        withCredentials([file(credentialsId: params.BUILD_CA_CREDENTIALS_ID, variable: 'BUILD_CA_FILE')]) { sh buildCommands }
                    } else { sh buildCommands }
                }
            }
        }
        stage('Push image') {
            steps {
                sh '''
                    set -eu
                    docker push "$IMAGE_REF"
                    printf '%s\n' "$IMAGE_REF" > image-reference.txt
                    docker image inspect "$IMAGE_REF" --format '{{range .RepoDigests}}{{println .}}{{end}}' > image-digest.txt
                '''
                archiveArtifacts artifacts: 'image-reference.txt,image-digest.txt', fingerprint: true
            }
        }
    }
    post {
        always {
            sh '''
                set +x
                if [ -n "${MIGRATOR_DOCKER_CONFIG:-}" ] && [ -d "$MIGRATOR_DOCKER_CONFIG" ]; then
                    docker logout "$REGISTRY_HOST" >/dev/null 2>&1 || true
                    rm -rf -- "$MIGRATOR_DOCKER_CONFIG"
                fi
            '''
        }
    }
}
