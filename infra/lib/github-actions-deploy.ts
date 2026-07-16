import { Duration, Stack } from "aws-cdk-lib";
import {
  FederatedPrincipal,
  OpenIdConnectProvider,
  PolicyStatement,
  Role,
} from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";

export interface GithubActionsDeployProps {
  githubOidcSubject: string;
}

export class GithubActionsDeploy extends Construct {
  readonly role: Role;

  constructor(
    scope: Construct,
    id: string,
    props: GithubActionsDeployProps,
  ) {
    super(scope, id);

    const stack = Stack.of(this);
    const providerArn = stack.formatArn({
      service: "iam",
      region: "",
      resource: "oidc-provider",
      resourceName: "token.actions.githubusercontent.com",
    });
    const provider = OpenIdConnectProvider.fromOpenIdConnectProviderArn(
      this,
      "GithubOidcProvider",
      providerArn,
    );

    this.role = new Role(this, "Role", {
      roleName: `${stack.stackName}-github-actions-deploy`,
      description: "Repository-scoped GitHub Actions role for CDK deployment",
      maxSessionDuration: Duration.hours(1),
      assumedBy: new FederatedPrincipal(
        provider.openIdConnectProviderArn,
        {
          StringEquals: {
            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
            "token.actions.githubusercontent.com:sub": props.githubOidcSubject,
          },
        },
        "sts:AssumeRoleWithWebIdentity",
      ),
    });

    const bootstrapRolePrefix =
      `arn:${stack.partition}:iam::${stack.account}:role/` +
      `cdk-hnb659fds`;
    this.role.addToPolicy(
      new PolicyStatement({
        actions: ["sts:AssumeRole", "sts:TagSession"],
        resources: [
          `${bootstrapRolePrefix}-deploy-role-${stack.account}-${stack.region}`,
          `${bootstrapRolePrefix}-file-publishing-role-${stack.account}-${stack.region}`,
          `${bootstrapRolePrefix}-image-publishing-role-${stack.account}-${stack.region}`,
          `${bootstrapRolePrefix}-lookup-role-${stack.account}-${stack.region}`,
        ],
      }),
    );
  }
}
