<div align="center">
  <img src="assets/logo-banner.png" alt="OpenTranscribe Logo" width="400">

  # Backend Documentation Index
</div>

<!-- Updated for v0.4.0 -->

Comprehensive documentation for the OpenTranscribe backend system, organized by component and use case.

## 📚 Documentation Structure

### 🏠 [Main Backend README](backend/README.md)
**Start here for backend overview and quick setup**
- Quick start guide
- Architecture overview
- Development workflow
- API access points
- Testing and deployment

### 🏗️ [Application Architecture](backend/app/README.md)
**Deep dive into application structure and design patterns**
- Layered architecture explanation
- Directory structure and organization
- Request flow and data flow
- Development guidelines and patterns

## 📖 Component Documentation

### 🔐 Authentication Module
**Enterprise authentication with multiple identity providers**

The `backend/app/auth/` module provides flexible authentication supporting all methods simultaneously (hybrid auth):
- **Local Authentication** (`direct_auth.py`): bcrypt_sha256 password hashing
- **LDAP/Active Directory** (`ldap_auth.py`): Enterprise directory integration
- **OIDC/Keycloak** (`keycloak_auth.py`): OAuth 2.0 with PKCE flow
- **PKI/X.509** (`pki_auth.py`): Certificate-based authentication (CAC/PIV)

Auth method configuration is stored in the database (Admin UI → Settings → Authentication) and takes precedence over `.env` variables.

**Security Components:**
- `mfa.py` - TOTP multi-factor authentication with backup codes
- `password_policy.py` - Configurable password complexity rules
- `password_history.py` - Password reuse prevention
- `lockout.py` - Account lockout after failed attempts
- `rate_limit.py` - Authentication rate limiting
- `session.py` - Session management with token rotation
- `audit.py` - Authentication event logging
- `token_service.py` - JWT token management

**Configuration Guides:**
- [LDAP Authentication](LDAP_AUTH.md) - Active Directory setup
- [OpenID Connect Setup](OIDC_SETUP.md) - OAuth 2.0 / OIDC SSO
- [PKI Authentication](PKI_SETUP.md) - Certificate authentication
- [Security Overview](SECURITY.md) - Security features and FedRAMP compliance
- [Testing Checklist](TESTING_CHECKLIST.md) - Authentication verification

### 🌐 [API Layer](backend/app/api/README.md)
**Complete API documentation and patterns**
- RESTful endpoint design
- Authentication and authorization
- Request/response patterns
- Error handling standards
- WebSocket integration
- Adding new endpoints

### 🗄️ [Data Models](backend/app/models/README.md)
**Database schema and ORM models**
- Database design overview
- Entity relationships
- Model definitions and constraints
- Query patterns and optimization
- Migration strategies

### 🔧 [Services Layer](backend/app/services/README.md)
**Business logic and service patterns**
- Service layer principles
- File management service
- Transcription workflow service
- External service integration
- Error handling and transactions

### ⚡ [Background Tasks](backend/app/tasks/README.md)
**Asynchronous processing and AI workflows**
- Task system architecture
- Transcription pipeline: unified 3-stage Celery chain (preprocess → GPU → postprocess)
- Analytics and summarization
- Task monitoring and error handling
- Performance optimization
- Selective reprocessing: stage picker for re-running specific pipeline stages

### 🎙️ ASR Module
**Cloud and local ASR provider abstraction** (`backend/app/services/asr/`)
- 10 providers: local (WhisperX), Deepgram, AssemblyAI, OpenAI, Google, Azure, AWS, Speechmatics, Gladia, pyannote.ai
- Admin-pinned local model (`asr.local_model` system setting, overrides `WHISPER_MODEL` env var)
- Per-user cloud provider configuration with encrypted API key storage
- `DEPLOYMENT_MODE=lite` disables local GPU workers entirely

**Hybrid Mode (CPU Transcription + GPU/MPS Diarization)**

Auto-activates when the GPU lacks VRAM for the configured model, or always on macOS (MPS).

- Detection: `HardwareInfo.should_use_hybrid_mode(model_name)` in `app/utils/hardware_detection.py`
- VRAM threshold: minimum batch=2 peak for model (turbo=3893 MB, medium=3829 MB, small=2933 MB) vs. 80% of total GPU VRAM
- When active: transcription → CPU with `WHISPER_HYBRID_CPU_MODEL` (default `small`, int8), diarization → GPU/MPS unchanged
- Config field: `TranscriptionConfig.diarization_device` separates diarization device from transcription device
- Override: `WHISPER_HYBRID_MODE=auto|true|false` (auto = default, true = always, false = never)
- Benchmark data: `docs/whisper-vram-profile/README.md`

### 🛠️ [Utilities](backend/app/utils/README.md)
**Common utilities and helper functions**
- Authentication decorators
- Database helpers
- Error handling utilities
- Task management utilities
- Testing patterns

## 📋 Specialized Documentation

### 🔒 [Security Documentation](SECURITY.md)
**Security features and compliance**
- Authentication methods (Local, LDAP, OIDC, PKI)
- Multi-factor authentication (MFA/TOTP)
- Password policies and account lockout
- Session management and rate limiting
- Audit logging
- FedRAMP compliance features (AC-8, IA-2, IA-5, AC-12, AU-2/AU-3)

### 🗃️ [Database Strategy](backend/app/db/README.md)
**Database management approach**
- Development vs production workflows
- Alembic migration strategy
- Schema change procedures
- Troubleshooting guide

### 📁 [Utility Scripts](backend/scripts/README.md)
**Administrative and development scripts**
- Admin user creation
- Database inspection tools
- Debugging utilities
- Setup scripts

### Files Removed in v0.4.0

The following files were removed as part of the pipeline unification refactor. Any documentation or code references to them should be updated:

| Removed File | Replacement |
|---|---|
| `whisperx_service.py` | Unified ASR provider abstraction in `app/services/asr/` |
| `parallel_pipeline.py` | Celery chain: preprocess → GPU → postprocess |
| `fast_speaker_assignment.py` | Folded into unified GPU stage |
| `batched_alignment.py` | Removed — word timestamps are native in faster-whisper DTW |
| `pyannote_compat.py` | PyAnnote v4 API used directly (no compat shim needed) |

Word-level timestamps are now produced natively by faster-whisper via cross-attention DTW. `ENABLE_ALIGNMENT` and `TRANSCRIPTION_ENGINE` environment variables are silently ignored.

## 🚀 Getting Started Guides

### For New Developers
1. **[Backend README](backend/README.md)** - Start here for environment setup
2. **[Application Architecture](backend/app/README.md)** - Understand the codebase structure
3. **[API Documentation](backend/app/api/README.md)** - Learn the API patterns
4. **[Adding Features Guide](#adding-new-features)** - Step-by-step feature development

### For API Integration
1. **[API Layer Documentation](backend/app/api/README.md)**
2. **[Authentication Patterns](backend/app/utils/README.md#authentication-decorators)**
3. **[Error Handling](backend/app/utils/README.md#error-handlers)**
4. **Interactive API Docs**: http://localhost:5174/api/docs

### For Database Work
1. **[Data Models](backend/app/models/README.md)**
2. **[Database Strategy](backend/app/db/README.md)**
3. **[Database Helpers](backend/app/utils/README.md#database-helpers)**
4. **[Migration Guide](backend/app/db/README.md#migration-strategy)**

### For Background Processing
1. **[Tasks Overview](backend/app/tasks/README.md)**
2. **[Transcription Pipeline](backend/app/tasks/README.md#transcription-pipeline)**
3. **[Task Monitoring](backend/app/tasks/README.md#task-monitoring)**
4. **Flower Dashboard**: http://localhost:5175/flower

## 🔧 Development Workflows

### Adding New Features

#### 1. Planning Phase
- Review **[Application Architecture](backend/app/README.md)** for patterns
- Check **[API Documentation](backend/app/api/README.md)** for endpoint conventions
- Review **[Data Models](backend/app/models/README.md)** for database design

#### 2. Implementation Phase
```bash
# Follow this order for new features:
1. Update database schema (models/ + DATABASE_APPROACH.md)
2. Create/update Pydantic schemas (schemas/)
3. Implement business logic (services/)
4. Create API endpoints (api/endpoints/)
5. Add background tasks if needed (tasks/)
6. Write comprehensive tests (tests/)
```

#### 3. Documentation Phase
- Update relevant README files
- Add docstrings to all new functions/classes
- Update API documentation examples
- Add any new patterns to architecture docs

### Debugging Workflows

#### API Issues
1. Check **[Error Handling](backend/app/utils/README.md#error-handlers)** patterns
2. Review **[API Documentation](backend/app/api/README.md)** for debugging tips
3. Use interactive docs at http://localhost:5174/api/docs
4. Check logs: `./opentr.sh logs backend`

#### Database Issues
1. Review **[Database Strategy](backend/app/db/README.md#troubleshooting)**
2. Use **[Database Scripts](backend/scripts/README.md)** for inspection
3. Check **[Query Patterns](backend/app/models/README.md#query-patterns)**
4. Run: `python scripts/db_inspect.py`

#### Background Task Issues
1. Check **[Task Documentation](backend/app/tasks/README.md#error-handling)**
2. Monitor via **Flower Dashboard**: http://localhost:5175/flower
3. Review **[Task Utilities](backend/app/utils/README.md#task-utilities)**
4. Check logs: `./opentr.sh logs celery-worker`

## 📊 Reference Materials

### API Reference
- **Interactive Docs**: http://localhost:5174/api/docs
- **ReDoc**: http://localhost:5174/api/redoc
- **[Endpoint List](backend/app/api/README.md#api-endpoints-reference)**
- **[Authentication Guide](backend/app/api/README.md#authentication--authorization)**

### Database Reference
- **[Schema Overview](backend/app/models/README.md#database-schema-overview)**
- **[Model Definitions](backend/app/models/README.md)**
- **[Query Examples](backend/app/models/README.md#query-patterns)**

### Task Reference
- **[Available Tasks](backend/app/tasks/README.md)**
- **[Progress Tracking](backend/app/tasks/README.md#task-monitoring)**
- **[Error Recovery](backend/app/tasks/README.md#error-handling)**

### Utility Reference
- **[Helper Functions](backend/app/utils/README.md)**
- **[Common Patterns](backend/app/utils/README.md#common-patterns)**
- **[Testing Utilities](backend/app/utils/README.md#testing-utilities)**

## 🧪 Testing Documentation

### Test Organization
```bash
tests/
├── api/endpoints/          # API endpoint tests
├── services/              # Service layer tests
├── models/               # Database model tests
├── tasks/                # Background task tests
└── utils/                # Utility function tests
```

### Running Tests
```bash
# All tests
./opentr.sh shell backend
pytest tests/

# Specific test categories
pytest tests/api/           # API tests
pytest tests/services/      # Service tests
pytest tests/models/        # Model tests

# With coverage
pytest --cov=app tests/
```

### Test Documentation Links
- **[API Testing](backend/app/api/README.md#testing-endpoints)**
- **[Service Testing](backend/app/services/README.md#testing-services)**
- **[Model Testing](backend/app/models/README.md#testing-models)**
- **[Task Testing](backend/app/tasks/README.md#testing-tasks)**

## 🚀 Deployment Documentation

### Environment Setup
- **[Production Setup](backend/README.md#deployment)**
- **[Environment Variables](backend/README.md#environment-variables)**
- **[Health Checks](backend/README.md#health-checks)**

### Database Deployment
- **[Migration Strategy](backend/app/db/README.md#production-approach)**
- **[Backup Procedures](backend/app/db/README.md#best-practices)**

### Task System Deployment
- **[Worker Configuration](backend/app/tasks/README.md#task-configuration)**
- **[Monitoring Setup](backend/app/tasks/README.md#task-monitoring)**
- **[Performance Tuning](backend/app/tasks/README.md#performance-optimization)**

## 🤝 Contributing Guidelines

### Code Standards
- **[Development Guidelines](backend/app/README.md#development-guidelines)**
- **[Code Organization Rules](backend/app/README.md#code-organization-rules)**
- **[Import Organization](backend/app/README.md#import-organization)**

### Documentation Standards
- **Google-style docstrings** for all functions and classes
- **Type hints** throughout the codebase
- **README updates** for new components
- **API documentation** for new endpoints

### Review Checklist
- [ ] Code follows established patterns
- [ ] Tests added for new functionality
- [ ] Documentation updated
- [ ] Type hints included
- [ ] Error handling implemented
- [ ] Performance considered

## 📞 Support and Resources

### Getting Help
- **[Main README](backend/README.md#support)** for general questions
- **[Troubleshooting Guide](backend/app/db/README.md#troubleshooting)**
- **[GitHub Issues](https://github.com/your-repo/issues)** for bug reports

### Useful Commands
```bash
# Development
./opentr.sh start dev           # Start development environment
./opentr.sh logs backend        # View backend logs
./opentr.sh shell backend       # Access backend container

# Database
./opentr.sh reset dev           # Reset development database
python scripts/db_inspect.py    # Inspect database state

# Testing
pytest tests/                   # Run all tests
pytest --cov=app tests/         # Run with coverage

# Monitoring
# Flower: http://localhost:5175/flower
# API Docs: http://localhost:5174/api/docs
```

---

**This documentation is living documentation - please keep it updated as the system evolves!**

Last updated: 2026-03-22
Backend version: OpenTranscribe v0.4.0
Python: 3.11+
FastAPI: 0.100+
