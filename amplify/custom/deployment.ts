import { RemovalPolicy, Stack, Token } from 'aws-cdk-lib';

/**
 * Per-deployment settings shared by backend.ts and the custom stacks.
 *
 * deploymentId
 *   A short id that is different for every deployment of GROW2 in an account
 *   (each developer's sandbox, dev, prod). It is the trailing hash Amplify puts
 *   in the root stack name (`amplify-<app>-<identifier>-sandbox-<hash>`), so it
 *   is stable across redeploys of the same stack. Every resource whose name must
 *   be unique in the account/region — CloudFormation export names, AgentCore
 *   runtime names, the Bedrock Guardrail, the WAF WebACL — is suffixed with it.
 *   Without this, a second deployment in the same region fails on name clashes,
 *   which is what forced the "one deployment per region" rule.
 *
 * env
 *   `dev` (default) or `prod`, from the GROW2_ENV environment variable the deploy
 *   script passes through. `prod` keeps data on stack deletion, enables
 *   point-in-time recovery and deletion protection on every table, and skips the
 *   demo user. `dev` keeps the existing teardown-friendly behaviour.
 */
export interface DeploymentConfig {
  readonly deploymentId: string;
  readonly exportPrefix: string;
  readonly env: 'dev' | 'prod';
  readonly isProd: boolean;
  readonly removalPolicy: RemovalPolicy;
  readonly autoDeleteObjects: boolean;
}

export function getDeploymentConfig(rootStack: Stack): DeploymentConfig {
  const stackName = rootStack.stackName;
  // Amplify sets a literal stack name on the root stack; fall back to a fixed
  // id (the pre-Phase-2 behaviour) if it is ever a token.
  const deploymentId = Token.isUnresolved(stackName)
    ? 'main'
    : stackName.split('-').pop()!.toLowerCase().replace(/[^a-z0-9]/g, '') || 'main';

  const rawEnv = (process.env.GROW2_ENV || 'dev').toLowerCase();
  if (rawEnv !== 'dev' && rawEnv !== 'prod') {
    throw new Error(`GROW2_ENV must be "dev" or "prod", got "${process.env.GROW2_ENV}"`);
  }
  const isProd = rawEnv === 'prod';

  return {
    deploymentId,
    exportPrefix: `GROW2-${deploymentId}`,
    env: rawEnv,
    isProd,
    removalPolicy: isProd ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY,
    autoDeleteObjects: !isProd,
  };
}
